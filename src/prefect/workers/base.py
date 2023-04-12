import abc
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Type, Union
from uuid import uuid4

import anyio
import anyio.abc
import pendulum
from pydantic import BaseModel, Field, PrivateAttr, validator

import prefect
from prefect._internal.compatibility.experimental import experimental
from prefect.client.orchestration import PrefectClient, get_client
from prefect.engine import propose_state
from prefect.events import emit_event
from prefect.events.related import object_as_related_resource, tags_as_related_resources
from prefect.events.schemas import RelatedResource
from prefect.exceptions import Abort, ObjectNotFound
from prefect.logging.loggers import get_logger
from prefect.server import schemas
from prefect.server.schemas.actions import WorkPoolUpdate
from prefect.settings import PREFECT_WORKER_PREFETCH_SECONDS, get_current_settings
from prefect.states import Crashed, Pending, exception_to_failed_state
from prefect.utilities.dispatch import register_base_type
from prefect.utilities.slugify import slugify
from prefect.utilities.templating import apply_values, resolve_block_document_references

if TYPE_CHECKING:
    from prefect.client.schemas import FlowRun
    from prefect.server.schemas.core import Flow
    from prefect.server.schemas.responses import (
        DeploymentResponse,
        WorkerFlowRunResponse,
    )


class BaseJobConfiguration(BaseModel):
    command: Optional[str] = Field(
        default=None,
        description=(
            "The command to use when starting a flow run. "
            "In most cases, this should be left blank and the command "
            "will be automatically generated by the worker."
        ),
    )
    env: Dict[str, Optional[str]] = Field(
        default_factory=dict,
        title="Environment Variables",
        description="Environment variables to set when starting a flow run.",
    )
    labels: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Labels applied to infrastructure created by the worker using "
            "this job configuration."
        ),
    )
    name: Optional[str] = Field(
        default=None,
        description=(
            "Name given to infrastructure created by the worker using this "
            "job configuration."
        ),
    )

    _related_objects: Dict[str, Any] = PrivateAttr(default_factory=dict)

    @validator("command")
    def _coerce_command(cls, v):
        """Make sure that empty strings are treated as None"""
        if not v:
            return None
        return v

    @staticmethod
    def _get_base_config_defaults(variables: dict) -> dict:
        """Get default values from base config for all variables that have them."""
        defaults = dict()
        for variable_name, attrs in variables.items():
            if "default" in attrs:
                defaults[variable_name] = attrs["default"]

        return defaults

    @classmethod
    async def from_template_and_values(cls, base_job_template: dict, values: dict):
        """Creates a valid worker configuration object from the provided base
        configuration and overrides.

        Important: this method expects that the base_job_template was already
        validated server-side.
        """
        job_config: Dict[str, Any] = base_job_template["job_configuration"]
        variables_schema = base_job_template["variables"]
        variables = cls._get_base_config_defaults(
            variables_schema.get("properties", {})
        )
        variables.update(values)
        variables = await resolve_block_document_references(variables)

        populated_configuration = apply_values(job_config, variables)
        return cls(**populated_configuration)

    @classmethod
    def json_template(cls) -> dict:
        """Returns a dict with job configuration as keys and the corresponding templates as values

        Defaults to using the job configuration parameter name as the template variable name.

        e.g.
        {
            key1: '{{ key1 }}',     # default variable template
            key2: '{{ template2 }}', # `template2` specifically provide as template
        }
        """
        configuration = {}
        properties = cls.schema()["properties"]
        for k, v in properties.items():
            if v.get("template"):
                template = v["template"]
            else:
                template = "{{ " + k + " }}"
            configuration[k] = template

        return configuration

    def prepare_for_flow_run(
        self,
        flow_run: "FlowRun",
        deployment: Optional["DeploymentResponse"] = None,
        flow: Optional["Flow"] = None,
    ):
        self._related_objects = {
            "deployment": deployment,
            "flow": flow,
            "flow-run": flow_run,
        }

        if deployment is not None:
            deployment_labels = self._base_deployment_labels(deployment)
        else:
            deployment_labels = {}

        if flow is not None:
            flow_labels = self._base_flow_labels(flow)
        else:
            flow_labels = {}

        env = {
            **self._base_environment(),
            **self._base_flow_run_environment(flow_run),
            **self.env,
        }
        self.env = {key: value for key, value in env.items() if value is not None}
        self.labels = {
            **self._base_flow_run_labels(flow_run),
            **deployment_labels,
            **flow_labels,
            **self.labels,
        }
        self.name = self.name or flow_run.name
        self.command = self.command or self._base_flow_run_command()

    @staticmethod
    def _base_flow_run_command() -> str:
        """
        Generate a command for a flow run job.
        """
        return "python -m prefect.engine"

    @staticmethod
    def _base_flow_run_labels(flow_run: "FlowRun") -> Dict[str, str]:
        """
        Generate a dictionary of labels for a flow run job.
        """
        return {
            "prefect.io/flow-run-id": str(flow_run.id),
            "prefect.io/flow-run-name": flow_run.name,
            "prefect.io/version": prefect.__version__,
        }

    @classmethod
    def _base_environment(cls) -> Dict[str, str]:
        """
        Environment variables that should be passed to all created infrastructure.

        These values should be overridable with the `env` field.
        """
        return get_current_settings().to_environment_variables(exclude_unset=True)

    @staticmethod
    def _base_flow_run_environment(flow_run: "FlowRun") -> Dict[str, str]:
        """
        Generate a dictionary of environment variables for a flow run job.
        """
        return {"PREFECT__FLOW_RUN_ID": flow_run.id.hex}

    @staticmethod
    def _base_deployment_labels(deployment: "DeploymentResponse") -> Dict[str, str]:
        labels = {
            "prefect.io/deployment-id": str(deployment.id),
            "prefect.io/deployment-name": deployment.name,
        }
        if deployment.updated is not None:
            labels["prefect.io/deployment-updated"] = deployment.updated.in_timezone(
                "utc"
            ).to_iso8601_string()
        return labels

    @staticmethod
    def _base_flow_labels(flow: "Flow") -> Dict[str, str]:
        return {
            "prefect.io/flow-id": str(flow.id),
            "prefect.io/flow-name": flow.name,
        }

    def _related_resources(self) -> List[RelatedResource]:
        tags = set()
        related = []

        for kind, obj in self._related_objects.items():
            if hasattr(obj, "tags"):
                tags.update(obj.tags)
            related.append(object_as_related_resource(kind=kind, role=kind, object=obj))

        return related + tags_as_related_resources(tags)


class BaseVariables(BaseModel):
    name: Optional[str] = Field(
        default=None,
        description="Name given to infrastructure created by a worker.",
    )
    env: Dict[str, Optional[str]] = Field(
        default_factory=dict,
        title="Environment Variables",
        description="Environment variables to set when starting a flow run.",
    )
    labels: Dict[str, str] = Field(
        default_factory=dict,
        description="Labels applied to infrastructure created by a worker.",
    )
    command: Optional[str] = Field(
        default=None,
        description=(
            "The command to use when starting a flow run. "
            "In most cases, this should be left blank and the command "
            "will be automatically generated by the worker."
        ),
    )


class BaseWorkerResult(BaseModel, abc.ABC):
    identifier: str
    status_code: int

    def __bool__(self):
        return self.status_code == 0


@register_base_type
class BaseWorker(abc.ABC):
    type: str
    job_configuration: Type[BaseJobConfiguration] = BaseJobConfiguration
    job_configuration_variables: Optional[Type[BaseVariables]] = None

    _documentation_url = ""
    _logo_url = ""
    _description = ""

    @experimental(feature="The workers feature", group="workers")
    def __init__(
        self,
        work_pool_name: str,
        work_queues: Optional[List[str]] = None,
        name: Optional[str] = None,
        prefetch_seconds: Optional[float] = None,
        create_pool_if_not_found: bool = True,
        limit: Optional[int] = None,
    ):
        """
        Base class for all Prefect workers.

        Args:
            name: The name of the worker. If not provided, a random one
                will be generated. If provided, it cannot contain '/' or '%'.
                The name is used to identify the worker in the UI; if two
                processes have the same name, they will be treated as the same
                worker.
            work_pool_name: The name of the work pool to poll.
            work_queues: A list of work queues to poll. If not provided, all
                work queue in the work pool will be polled.
            prefetch_seconds: The number of seconds to prefetch flow runs for.
            create_pool_if_not_found: Whether to create the work pool
                if it is not found. Defaults to `True`, but can be set to `False` to
                ensure that work pools are not created accidentally.
            limit: The maximum number of flow runs this worker should be running at
                a given time.
        """
        if name and ("/" in name or "%" in name):
            raise ValueError("Worker name cannot contain '/' or '%'")
        self.name = name or f"{self.__class__.__name__} {uuid4()}"
        self._logger = get_logger(f"worker.{self.__class__.type}.{self.name.lower()}")

        self.is_setup = False
        self._create_pool_if_not_found = create_pool_if_not_found
        self._work_pool_name = work_pool_name
        self._work_queues: Set[str] = set(work_queues) if work_queues else set()

        self._prefetch_seconds: float = (
            prefetch_seconds or PREFECT_WORKER_PREFETCH_SECONDS.value()
        )

        self._work_pool: Optional[schemas.core.WorkPool] = None
        self._runs_task_group: Optional[anyio.abc.TaskGroup] = None
        self._client: Optional[PrefectClient] = None
        self._limit = limit
        self._limiter: Optional[anyio.CapacityLimiter] = None
        self._submitting_flow_run_ids = set()

    @classmethod
    def get_documentation_url(cls) -> str:
        return cls._documentation_url

    @classmethod
    def get_logo_url(cls) -> str:
        return cls._logo_url

    @classmethod
    def get_description(cls) -> str:
        return cls._description

    @classmethod
    def get_default_base_job_template(cls) -> Dict:
        if cls.job_configuration_variables is None:
            schema = cls.job_configuration.schema()
            # remove "template" key from all dicts in schema['properties'] because it is not a
            # relevant field
            for key, value in schema["properties"].items():
                if isinstance(value, dict):
                    schema["properties"][key].pop("template", None)
            variables_schema = schema
        else:
            variables_schema = cls.job_configuration_variables.schema()
        variables_schema.pop("title", None)
        return {
            "job_configuration": cls.job_configuration.json_template(),
            "variables": variables_schema,
        }

    def get_name_slug(self):
        return slugify(self.name)

    @abc.abstractmethod
    async def run(
        self,
        flow_run: "FlowRun",
        configuration: BaseJobConfiguration,
        task_status: Optional[anyio.abc.TaskStatus] = None,
    ) -> BaseWorkerResult:
        """
        Runs a given flow run on the current worker.
        """
        raise NotImplementedError(
            "Workers must implement a method for running submitted flow runs"
        )

    @classmethod
    def __dispatch_key__(cls):
        if cls.__name__ == "BaseWorker":
            return None  # The base class is abstract
        return cls.type

    async def setup(self):
        """Prepares the worker to run."""
        self._logger.debug("Setting up worker...")
        self._runs_task_group = anyio.create_task_group()
        self._limiter = (
            anyio.CapacityLimiter(self._limit) if self._limit is not None else None
        )
        self._client = get_client()
        await self._client.__aenter__()
        await self._runs_task_group.__aenter__()

        self.is_setup = True

    async def teardown(self, *exc_info):
        """Cleans up resources after the worker is stopped."""
        self._logger.debug("Tearing down worker...")
        self.is_setup = False
        if self._runs_task_group:
            await self._runs_task_group.__aexit__(*exc_info)
        if self._client:
            await self._client.__aexit__(*exc_info)
        self._runs_task_group = None
        self._client = None

    async def get_and_submit_flow_runs(self):
        runs_response = await self._get_scheduled_flow_runs()
        return await self._submit_scheduled_flow_runs(flow_run_response=runs_response)

    async def _update_local_work_pool_info(self):
        try:
            work_pool = await self._client.read_work_pool(
                work_pool_name=self._work_pool_name
            )
        except ObjectNotFound:
            if self._create_pool_if_not_found:
                work_pool = await self._client.create_work_pool(
                    work_pool=schemas.actions.WorkPoolCreate(
                        name=self._work_pool_name, type=self.type
                    )
                )
                self._logger.info(f"Worker pool {self._work_pool_name!r} created.")
            else:
                self._logger.warning(f"Worker pool {self._work_pool_name!r} not found!")
                return

        # if the remote config type changes (or if it's being loaded for the
        # first time), check if it matches the local type and warn if not
        if getattr(self._work_pool, "type", 0) != work_pool.type:
            if work_pool.type != self.__class__.type:
                self._logger.warning(
                    "Worker type mismatch! This worker process expects type "
                    f"{self.type!r} but received {work_pool.type!r}"
                    " from the server. Unexpected behavior may occur."
                )

        # once the work pool is loaded, verify that it has a `base_job_template` and
        # set it if not
        if not work_pool.base_job_template:
            job_template = self.__class__.get_default_base_job_template()
            await self._set_work_pool_template(work_pool, job_template)
            work_pool.base_job_template = job_template

        self._work_pool = work_pool

    async def _send_worker_heartbeat(self):
        if self._work_pool:
            await self._client.send_worker_heartbeat(
                work_pool_name=self._work_pool_name, worker_name=self.name
            )

    async def sync_with_backend(self):
        """
        Updates the worker's local information about it's current work pool and
        queues. Sends a worker heartbeat to the API.
        """
        await self._update_local_work_pool_info()

        await self._send_worker_heartbeat()

        self._logger.debug("Worker synchronized with the Prefect API server.")

    async def _get_scheduled_flow_runs(
        self,
    ) -> List["WorkerFlowRunResponse"]:
        """
        Retrieve scheduled flow runs from the work pool's queues.
        """
        scheduled_before = pendulum.now("utc").add(seconds=int(self._prefetch_seconds))
        self._logger.debug(
            f"Querying for flow runs scheduled before {scheduled_before}"
        )
        try:
            scheduled_flow_runs = (
                await self._client.get_scheduled_flow_runs_for_work_pool(
                    work_pool_name=self._work_pool_name,
                    scheduled_before=scheduled_before,
                    work_queue_names=list(self._work_queues),
                )
            )
            self._logger.debug(
                f"Discovered {len(scheduled_flow_runs)} scheduled_flow_runs"
            )
            return scheduled_flow_runs
        except ObjectNotFound:
            # the pool doesn't exist; it will be created on the next
            # heartbeat (or an appropriate warning will be logged)
            return []

    async def _submit_scheduled_flow_runs(
        self, flow_run_response: List["WorkerFlowRunResponse"]
    ) -> List["FlowRun"]:
        """
        Takes a list of WorkerFlowRunResponses and submits the referenced flow runs
        for execution by the worker.
        """
        submittable_flow_runs = [entry.flow_run for entry in flow_run_response]
        submittable_flow_runs.sort(key=lambda run: run.next_scheduled_start_time)
        for flow_run in submittable_flow_runs:
            if flow_run.id in self._submitting_flow_run_ids:
                continue

            try:
                if self._limiter:
                    self._limiter.acquire_on_behalf_of_nowait(flow_run.id)
            except anyio.WouldBlock:
                self._logger.info(
                    f"Flow run limit reached; {self._limiter.borrowed_tokens} flow runs"
                    " in progress."
                )
                break
            else:
                self._logger.info(f"Submitting flow run '{flow_run.id}'")
                self._submitting_flow_run_ids.add(flow_run.id)
                self._runs_task_group.start_soon(
                    self._submit_run,
                    flow_run,
                )

        return list(
            filter(
                lambda run: run.id in self._submitting_flow_run_ids,
                submittable_flow_runs,
            )
        )

    async def _check_flow_run(self, flow_run: "FlowRun") -> None:
        """
        Performs a check on a submitted flow run to warn the user if the flow run
        was created from a deployment with a storage block.
        """
        if flow_run.deployment_id:
            deployment = await self._client.read_deployment(flow_run.deployment_id)
            if deployment.storage_document_id:
                raise ValueError(
                    f"Flow run {flow_run.id!r} was created from deployment"
                    f" {deployment.name!r} which is configured with a storage block."
                    " Workers currently only support local storage. Please use an"
                    " agent to execute this flow run."
                )

    async def _submit_run(self, flow_run: "FlowRun") -> None:
        """
        Submits a given flow run for execution by the worker.
        """
        try:
            await self._check_flow_run(flow_run)
        except ValueError:
            self._logger.exception(
                (
                    "Flow run %s did not pass checks and will not be submitted for"
                    " execution"
                ),
                flow_run.id,
            )
            self._submitting_flow_run_ids.remove(flow_run.id)
            return

        ready_to_submit = await self._propose_pending_state(flow_run)

        if ready_to_submit:
            readiness_result = await self._runs_task_group.start(
                self._submit_run_and_capture_errors, flow_run
            )

            if readiness_result and not isinstance(readiness_result, Exception):
                try:
                    await self._client.update_flow_run(
                        flow_run_id=flow_run.id,
                        infrastructure_pid=str(readiness_result),
                    )
                except Exception:
                    self._logger.exception(
                        "An error occurred while setting the `infrastructure_pid` on "
                        f"flow run {flow_run.id!r}. The flow run will "
                        "not be cancellable."
                    )

            self._logger.info(f"Completed submission of flow run '{flow_run.id}'")

        else:
            # If the run is not ready to submit, release the concurrency slot
            if self._limiter:
                self._limiter.release_on_behalf_of(flow_run.id)

        self._submitting_flow_run_ids.remove(flow_run.id)

    async def _submit_run_and_capture_errors(
        self, flow_run: "FlowRun", task_status: anyio.abc.TaskStatus = None
    ) -> Union[BaseWorkerResult, Exception]:
        try:
            # TODO: Add functionality to handle base job configuration and
            # job configuration variables when kicking off a flow run
            configuration = await self._get_configuration(flow_run)
            self._emit_flow_run_submitted_event(configuration)
            result = await self.run(
                flow_run=flow_run,
                task_status=task_status,
                configuration=configuration,
            )
        except Exception as exc:
            if not task_status._future.done():
                # This flow run was being submitted and did not start successfully
                self._logger.exception(
                    f"Failed to submit flow run '{flow_run.id}' to infrastructure."
                )
                # Mark the task as started to prevent agent crash
                task_status.started(exc)
                await self._propose_failed_state(flow_run, exc)
            else:
                self._logger.exception(
                    f"An error occurred while monitoring flow run '{flow_run.id}'. "
                    "The flow run will not be marked as failed, but an issue may have "
                    "occurred."
                )
            return exc
        finally:
            if self._limiter:
                self._limiter.release_on_behalf_of(flow_run.id)

        if not task_status._future.done():
            self._logger.error(
                f"Infrastructure returned without reporting flow run '{flow_run.id}' "
                "as started or raising an error. This behavior is not expected and "
                "generally indicates improper implementation of infrastructure. The "
                "flow run will not be marked as failed, but an issue may have occurred."
            )
            # Mark the task as started to prevent agent crash
            task_status.started()

        if result.status_code != 0:
            await self._propose_crashed_state(
                flow_run,
                (
                    "Flow run infrastructure exited with non-zero status code"
                    f" {result.status_code}."
                ),
            )

        return result

    def get_status(self):
        """
        Retrieves the status of the current worker including its name, current worker
        pool, the work pool queues it is polling, and its local settings.
        """
        return {
            "name": self.name,
            "work_pool": (
                self._work_pool.dict(json_compatible=True)
                if self._work_pool is not None
                else None
            ),
            "settings": {
                "prefetch_seconds": self._prefetch_seconds,
            },
        }

    async def _get_configuration(
        self,
        flow_run: "FlowRun",
    ) -> BaseJobConfiguration:
        deployment = await self._client.read_deployment(flow_run.deployment_id)
        flow = await self._client.read_flow(flow_run.flow_id)
        configuration = await self.job_configuration.from_template_and_values(
            base_job_template=self._work_pool.base_job_template,
            values=deployment.infra_overrides or {},
        )
        configuration.prepare_for_flow_run(
            flow_run=flow_run, deployment=deployment, flow=flow
        )
        return configuration

    async def _propose_pending_state(self, flow_run: "FlowRun") -> bool:
        state = flow_run.state
        try:
            state = await propose_state(
                self._client, Pending(), flow_run_id=flow_run.id
            )
        except Abort as exc:
            self._logger.info(
                (
                    f"Aborted submission of flow run '{flow_run.id}'. "
                    f"Server sent an abort signal: {exc}"
                ),
            )
            return False
        except Exception:
            self._logger.exception(
                f"Failed to update state of flow run '{flow_run.id}'",
            )
            return False

        if not state.is_pending():
            self._logger.info(
                (
                    f"Aborted submission of flow run '{flow_run.id}': "
                    f"Server returned a non-pending state {state.type.value!r}"
                ),
            )
            return False

        return True

    async def _propose_failed_state(self, flow_run: "FlowRun", exc: Exception) -> None:
        try:
            await propose_state(
                self._client,
                await exception_to_failed_state(message="Submission failed.", exc=exc),
                flow_run_id=flow_run.id,
            )
        except Abort:
            # We've already failed, no need to note the abort but we don't want it to
            # raise in the agent process
            pass
        except Exception:
            self._logger.error(
                f"Failed to update state of flow run '{flow_run.id}'",
                exc_info=True,
            )

    async def _propose_crashed_state(self, flow_run: "FlowRun", message: str) -> None:
        try:
            state = await propose_state(
                self._client,
                Crashed(message=message),
                flow_run_id=flow_run.id,
            )
        except Abort:
            # Flow run already marked as failed
            pass
        except Exception:
            self._logger.exception(
                f"Failed to update state of flow run '{flow_run.id}'"
            )
        else:
            if state.is_crashed():
                self._logger.info(
                    f"Reported flow run '{flow_run.id}' as crashed: {message}"
                )

    async def _set_work_pool_template(self, work_pool, job_template):
        """Updates the `base_job_template` for the worker's workerpool server side."""
        await self._client.update_work_pool(
            work_pool_name=work_pool.name,
            work_pool=WorkPoolUpdate(
                base_job_template=job_template,
            ),
        )

    async def __aenter__(self):
        self._logger.debug("Entering worker context...")
        await self.setup()
        return self

    async def __aexit__(self, *exc_info):
        self._logger.debug("Exiting worker context...")
        await self.teardown(*exc_info)

    def __repr__(self):
        return f"Worker(pool={self._work_pool_name!r}, name={self.name!r})"

    def _event_resource(self):
        return {
            "prefect.resource.id": f"prefect.worker.{self.type}.{self.get_name_slug()}",
            "prefect.resource.name": self.name,
            "prefect.version": prefect.__version__,
            "prefect.worker-type": self.type,
        }

    def _emit_flow_run_submitted_event(self, configuration: BaseJobConfiguration):
        related = configuration._related_resources()

        if self._work_pool:
            related.append(
                object_as_related_resource(
                    kind="work-pool", role="work-pool", object=self._work_pool
                )
            )

        emit_event(
            event=f"prefect.worker.submitted-flow-run",
            resource=self._event_resource(),
            related=related,
        )
