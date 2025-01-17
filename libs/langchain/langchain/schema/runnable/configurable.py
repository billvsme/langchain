from __future__ import annotations

from abc import abstractmethod
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Type,
    Union,
    cast,
)

from langchain.pydantic_v1 import BaseModel
from langchain.schema.runnable.base import Runnable, RunnableSerializable
from langchain.schema.runnable.config import (
    RunnableConfig,
    get_config_list,
    get_executor_for_config,
)
from langchain.schema.runnable.utils import (
    ConfigurableField,
    ConfigurableFieldSpec,
    Input,
    Output,
    gather_with_concurrency,
)


class DynamicRunnable(RunnableSerializable[Input, Output]):
    bound: RunnableSerializable[Input, Output]

    class Config:
        arbitrary_types_allowed = True

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return True

    @classmethod
    def get_lc_namespace(cls) -> List[str]:
        return cls.__module__.split(".")[:-1]

    @property
    def InputType(self) -> Type[Input]:
        return self.bound.InputType

    @property
    def OutputType(self) -> Type[Output]:
        return self.bound.OutputType

    @property
    def input_schema(self) -> Type[BaseModel]:
        return self.bound.input_schema

    @property
    def output_schema(self) -> Type[BaseModel]:
        return self.bound.output_schema

    @abstractmethod
    def _prepare(
        self, config: Optional[RunnableConfig] = None
    ) -> Runnable[Input, Output]:
        ...

    def invoke(
        self, input: Input, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Output:
        return self._prepare(config).invoke(input, config, **kwargs)

    async def ainvoke(
        self, input: Input, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Output:
        return await self._prepare(config).ainvoke(input, config, **kwargs)

    def batch(
        self,
        inputs: List[Input],
        config: Optional[Union[RunnableConfig, List[RunnableConfig]]] = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Optional[Any],
    ) -> List[Output]:
        configs = get_config_list(config, len(inputs))
        prepared = [self._prepare(c) for c in configs]

        if all(p is self.bound for p in prepared):
            return self.bound.batch(
                inputs, config, return_exceptions=return_exceptions, **kwargs
            )

        if not inputs:
            return []

        configs = get_config_list(config, len(inputs))

        def invoke(
            bound: Runnable[Input, Output],
            input: Input,
            config: RunnableConfig,
        ) -> Union[Output, Exception]:
            if return_exceptions:
                try:
                    return bound.invoke(input, config, **kwargs)
                except Exception as e:
                    return e
            else:
                return bound.invoke(input, config, **kwargs)

        # If there's only one input, don't bother with the executor
        if len(inputs) == 1:
            return cast(List[Output], [invoke(prepared[0], inputs[0], configs[0])])

        with get_executor_for_config(configs[0]) as executor:
            return cast(
                List[Output], list(executor.map(invoke, prepared, inputs, configs))
            )

    async def abatch(
        self,
        inputs: List[Input],
        config: Optional[Union[RunnableConfig, List[RunnableConfig]]] = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Optional[Any],
    ) -> List[Output]:
        configs = get_config_list(config, len(inputs))
        prepared = [self._prepare(c) for c in configs]

        if all(p is self.bound for p in prepared):
            return await self.bound.abatch(
                inputs, config, return_exceptions=return_exceptions, **kwargs
            )

        if not inputs:
            return []

        configs = get_config_list(config, len(inputs))

        async def ainvoke(
            bound: Runnable[Input, Output],
            input: Input,
            config: RunnableConfig,
        ) -> Union[Output, Exception]:
            if return_exceptions:
                try:
                    return await bound.ainvoke(input, config, **kwargs)
                except Exception as e:
                    return e
            else:
                return await bound.ainvoke(input, config, **kwargs)

        coros = map(ainvoke, prepared, inputs, configs)
        return await gather_with_concurrency(configs[0].get("max_concurrency"), *coros)

    def stream(
        self,
        input: Input,
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> Iterator[Output]:
        return self._prepare(config).stream(input, config, **kwargs)

    async def astream(
        self,
        input: Input,
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> AsyncIterator[Output]:
        async for chunk in self._prepare(config).astream(input, config, **kwargs):
            yield chunk

    def transform(
        self,
        input: Iterator[Input],
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> Iterator[Output]:
        return self._prepare(config).transform(input, config, **kwargs)

    async def atransform(
        self,
        input: AsyncIterator[Input],
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> AsyncIterator[Output]:
        async for chunk in self._prepare(config).atransform(input, config, **kwargs):
            yield chunk


class RunnableConfigurableFields(DynamicRunnable[Input, Output]):
    fields: Dict[str, ConfigurableField]

    @property
    def config_specs(self) -> Sequence[ConfigurableFieldSpec]:
        return [
            ConfigurableFieldSpec(
                id=spec.id,
                name=spec.name,
                description=spec.description
                or self.bound.__fields__[field_name].field_info.description,
                annotation=spec.annotation
                or self.bound.__fields__[field_name].annotation,
                default=getattr(self.bound, field_name),
            )
            for field_name, spec in self.fields.items()
        ]

    def configurable_fields(
        self, **kwargs: ConfigurableField
    ) -> RunnableSerializable[Input, Output]:
        return self.bound.configurable_fields(**{**self.fields, **kwargs})

    def _prepare(
        self, config: Optional[RunnableConfig] = None
    ) -> Runnable[Input, Output]:
        config = config or {}
        specs_by_id = {spec.id: (key, spec) for key, spec in self.fields.items()}
        configurable = {
            specs_by_id[k][0]: v
            for k, v in config.get("configurable", {}).items()
            if k in specs_by_id
        }

        if configurable:
            return self.bound.__class__(**{**self.bound.dict(), **configurable})
        else:
            return self.bound


class RunnableConfigurableAlternatives(DynamicRunnable[Input, Output]):
    which: ConfigurableField

    alternatives: Dict[str, RunnableSerializable[Input, Output]]

    @property
    def config_specs(self) -> Sequence[ConfigurableFieldSpec]:
        alt_keys = self.alternatives.keys()
        which_keys = tuple(Literal[k] for k in alt_keys) + (  # type: ignore
            Literal["default"],
        )
        return [
            ConfigurableFieldSpec(
                id=self.which.id,
                name=self.which.name,
                description=self.which.description,
                annotation=Union[which_keys],  # type: ignore
                default="default",
            ),
            *self.bound.config_specs,
        ] + [s for alt in self.alternatives.values() for s in alt.config_specs]

    def configurable_fields(
        self, **kwargs: ConfigurableField
    ) -> RunnableSerializable[Input, Output]:
        return self.__class__(
            which=self.which,
            bound=self.bound.configurable_fields(**kwargs),
            alternatives=self.alternatives,
        )

    def _prepare(
        self, config: Optional[RunnableConfig] = None
    ) -> Runnable[Input, Output]:
        config = config or {}
        which = config.get("configurable", {}).get(self.which.id)
        if not which:
            return self.bound
        elif which in self.alternatives:
            return self.alternatives[which]
        else:
            raise ValueError(f"Unknown alternative: {which}")
