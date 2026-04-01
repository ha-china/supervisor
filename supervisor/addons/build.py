"""Supervisor add-on build environment."""

from __future__ import annotations

import base64
from functools import cached_property
import json
import logging
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from awesomeversion import AwesomeVersion

from ..const import (
    ATTR_ARGS,
    ATTR_BUILD_FROM,
    ATTR_LABELS,
    ATTR_PASSWORD,
    ATTR_SQUASH,
    ATTR_USERNAME,
    FILE_SUFFIX_CONFIGURATION,
    LABEL_ARCH,
    LABEL_DESCRIPTION,
    LABEL_NAME,
    LABEL_TYPE,
    LABEL_URL,
    LABEL_VERSION,
    META_APP,
    SOCKET_DOCKER,
    CpuArch,
)
from ..coresys import CoreSys, CoreSysAttributes
from ..docker.const import DOCKER_HUB, DOCKER_HUB_LEGACY, DockerMount, MountType
from ..docker.interface import MAP_ARCH
from ..exceptions import (
    AddonBuildArchitectureNotSupportedError,
    AddonBuildDockerfileMissingError,
    ConfigurationFileError,
    HassioArchNotFound,
)
from ..utils.common import FileConfiguration, find_one_filetype
from .validate import SCHEMA_BUILD_CONFIG

if TYPE_CHECKING:
    from .manager import AnyAddon

_LOGGER: logging.Logger = logging.getLogger(__name__)


class AddonBuild(FileConfiguration, CoreSysAttributes):
    """Handle build options for add-ons."""

    def __init__(self, coresys: CoreSys, addon: AnyAddon) -> None:
        """Initialize Supervisor add-on builder."""
        self.coresys: CoreSys = coresys
        self.addon = addon
        self._has_build_file: bool = False

        # Search for build file later in executor
        super().__init__(None, SCHEMA_BUILD_CONFIG)

    @property
    def has_build_file(self) -> bool:
        """Return True if a build configuration file was found on disk."""
        return self._has_build_file

    def _get_build_file(self) -> Path:
        """Get build file.

        Must be run in executor.
        """
        try:
            result = find_one_filetype(
                self.addon.path_location, "build", FILE_SUFFIX_CONFIGURATION
            )
            self._has_build_file = True
            return result
        except ConfigurationFileError:
            self._has_build_file = False
            return self.addon.path_location / "build.json"

    async def read_data(self) -> None:
        """Load data from file."""
        if not self._file:
            self._file = await self.sys_run_in_executor(self._get_build_file)

        await super().read_data()

    async def save_data(self):
        """Ignore save function."""
        raise RuntimeError()

    @cached_property
    def arch(self) -> CpuArch:
        """Return arch of the add-on."""
        return self.sys_arch.match([self.addon.arch])

    @property
    def base_image(self) -> str | None:
        """Return base image for this add-on, or None to use Dockerfile default."""
        if not self._data[ATTR_BUILD_FROM]:
            if self._has_build_file:
                return "ghcr.io/home-assistant/base:latest"
            return None

        if isinstance(self._data[ATTR_BUILD_FROM], str):
            return self._data[ATTR_BUILD_FROM]

        # Evaluate correct base image
        if self.arch not in self._data[ATTR_BUILD_FROM]:
            raise HassioArchNotFound(
                f"Add-on {self.addon.slug} is not supported on {self.arch}"
            )
        return self._data[ATTR_BUILD_FROM][self.arch]

    @property
    def squash(self) -> bool:
        """Return True or False if squash is active."""
        return self._data[ATTR_SQUASH]

    @property
    def additional_args(self) -> dict[str, str]:
        """Return additional Docker build arguments."""
        return self._data[ATTR_ARGS]

    @property
    def additional_labels(self) -> dict[str, str]:
        """Return additional Docker labels."""
        return self._data[ATTR_LABELS]

    def get_dockerfile(self) -> Path:
        """Return Dockerfile path.

        Must be run in executor.
        """
        if self.addon.path_location.joinpath(f"Dockerfile.{self.arch}").exists():
            return self.addon.path_location.joinpath(f"Dockerfile.{self.arch}")
        return self.addon.path_location.joinpath("Dockerfile")

    async def is_valid(self) -> None:
        """Return true if the build env is valid."""

        def build_is_valid() -> bool:
            return all(
                [
                    self.addon.path_location.is_dir(),
                    self.get_dockerfile().is_file(),
                ]
            )

        try:
            if not await self.sys_run_in_executor(build_is_valid):
                raise AddonBuildDockerfileMissingError(
                    _LOGGER.error, addon=self.addon.slug
                )
        except HassioArchNotFound:
            raise AddonBuildArchitectureNotSupportedError(
                _LOGGER.error,
                addon=self.addon.slug,
                addon_arch_list=self.addon.supported_arch,
                system_arch_list=[arch.value for arch in self.sys_arch.supported],
            ) from None

    def _registry_key(self, registry: str) -> str:
        """Return the Docker config.json key for a registry."""
        if registry in (DOCKER_HUB, DOCKER_HUB_LEGACY):
            return "https://index.docker.io/v1/"
        return registry

    def _registry_auth(self, registry: str) -> str:
        """Return base64-encoded auth string for a registry."""
        stored = self.sys_docker.config.registries[registry]
        return base64.b64encode(
            f"{stored[ATTR_USERNAME]}:{stored[ATTR_PASSWORD]}".encode()
        ).decode()

    def get_docker_config_json(self) -> str | None:
        """Generate Docker config.json content with all configured registry credentials.

        Returns a JSON string with registry credentials, or None if no registries
        are configured.
        """
        if not self.sys_docker.config.registries:
            return None

        auths = {
            self._registry_key(registry): {"auth": self._registry_auth(registry)}
            for registry in self.sys_docker.config.registries
        }
        return json.dumps({"auths": auths})

    def get_docker_args(
        self, version: AwesomeVersion, image_tag: str, docker_config_path: Path | None
    ) -> dict[str, Any]:
        """Create a dict with Docker run args."""
        dockerfile_path = self.get_dockerfile().relative_to(self.addon.path_location)

        build_cmd = [
            "docker",
            "buildx",
            "build",
            ".",
            "--tag",
            image_tag,
            "--file",
            str(dockerfile_path),
            "--platform",
            MAP_ARCH[self.arch],
            "--pull",
        ]

        labels = {
            LABEL_VERSION: version,
            LABEL_ARCH: self.arch,
            LABEL_TYPE: META_APP,
            **self.additional_labels,
        }

        # Set name only if non-empty, could have been set in Dockerfile
        if name := self._fix_label("name"):
            labels[LABEL_NAME] = name

        # Set description only if non-empty, could have been set in Dockerfile
        if description := self._fix_label("description"):
            labels[LABEL_DESCRIPTION] = description

        if self.addon.url:
            labels[LABEL_URL] = self.addon.url

        for key, value in labels.items():
            build_cmd.extend(["--label", f"{key}={value}"])

        build_args = {
            "BUILD_VERSION": version,
            "BUILD_ARCH": self.arch,
            **self.additional_args,
        }

        if self.base_image is not None:
            build_args["BUILD_FROM"] = self.base_image

        for key, value in build_args.items():
            build_cmd.extend(["--build-arg", f"{key}={value}"])

        # The addon path will be mounted from the host system
        addon_extern_path = self.sys_config.local_to_extern_path(
            self.addon.path_location
        )

        mounts = [
            DockerMount(
                type=MountType.BIND,
                source=SOCKET_DOCKER.as_posix(),
                target="/var/run/docker.sock",
                read_only=False,
            ),
            DockerMount(
                type=MountType.BIND,
                source=addon_extern_path.as_posix(),
                target="/addon",
                read_only=True,
            ),
        ]

        # Mount Docker config with registry credentials if available
        if docker_config_path:
            docker_config_extern_path = self.sys_config.local_to_extern_path(
                docker_config_path
            )
            mounts.append(
                DockerMount(
                    type=MountType.BIND,
                    source=docker_config_extern_path.as_posix(),
                    target="/root/.docker/config.json",
                    read_only=True,
                )
            )

        return {
            "command": build_cmd,
            "mounts": mounts,
            "working_dir": PurePath("/addon"),
        }

    def _fix_label(self, label_name: str) -> str:
        """Remove characters they are not supported."""
        label = getattr(self.addon, label_name, "")
        return label.replace("'", "")
