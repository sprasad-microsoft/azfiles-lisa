from dataclasses import dataclass, field
from typing import List, Optional, Union, Dict, Any, Type
from dataclasses_json import dataclass_json
import re

from lisa import schema
from lisa.util import field_metadata, constants, LisaException
from lisa.node import RemoteNode
from lisa.parameter_parser.runbook import RunbookBuilder

from .common import (
    get_or_create_storage_container,
    generate_user_delegation_sas_token,
)
from .platform_ import AzurePlatform, AzurePlatformSchema
from .transformers import _load_platform
from lisa.transformers.deployment_transformer import (
    DeploymentTransformer,
    DeploymentTransformerSchema,
)

@dataclass_json
@dataclass
class FileTransferTransformerSchema(DeploymentTransformerSchema):
    connection: Optional[schema.RemoteNode] = field(
        default=None, metadata=field_metadata(required=False)
    )
    mode: str = field(default="upload")
    shared_resource_group_name: str = field(default="lisa_shared_resource_group")
    storage_account_name: str = field(default="")
    container_name: str = field(default="lisa-file-transfer")
    vm_directory: str = field(default="", metadata=field_metadata(required=True))
    blob_directory: str = field(default="", metadata=field_metadata(required=True))
    file_patterns: Optional[Union[str, List[str]]] = field(default=None)
    azcopy_path: str = field(default="")
    container_sas_token: str = field(default="")  # New field for user-provided container SAS

class FileTransferTransformer(DeploymentTransformer):
    __uploaded_urls = "uploaded_blob_urls"
    __downloaded_paths = "downloaded_vm_paths"

    @classmethod
    def type_name(cls) -> str:
        return "azure_file_transfer"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return FileTransferTransformerSchema

    @property
    def _output_names(self) -> List[str]:
        return [self.__uploaded_urls, self.__downloaded_paths]

    def _internal_run(self) -> Dict[str, Any]:
        runbook: FileTransferTransformerSchema = self.runbook
        print("DEBUG: runbook.connection =", getattr(self.runbook, "connection", None))
        print("DEBUG: build_vm_address =", getattr(self.runbook.connection, "address", None) if self.runbook.connection else None)
        print("DEBUG: private_key_file =", getattr(self.runbook.connection, "private_key_file", None) if self.runbook.connection else None)
        # Validate mode
        if runbook.mode not in ["upload", "download"]:
            raise LisaException(f"Invalid mode '{runbook.mode}'. Must be 'upload' or 'download'.")
        
        # Use the node from the deployment transformer
        node = self._node
        if not isinstance(node, RemoteNode):
            raise LisaException("Target node is not a RemoteNode")

        self._validate_names(runbook)
        self._ensure_internet(node)
        self._ensure_required_tools(node)
        self._ensure_sudo(node)
        self._ensure_directory_exists(node, runbook.vm_directory, runbook.mode)

        # Get platform for Azure operations
        platform = _load_platform(self._runbook_builder, self.type_name())

        if runbook.mode == "upload":
            urls = self._upload_files(runbook, platform, node)
            return {self.__uploaded_urls: urls}
        elif runbook.mode == "download":
            paths = self._download_files(runbook, platform, node)
            return {self.__downloaded_paths: paths}
        else:
            raise LisaException(f"Unknown mode: {runbook.mode}")

    def _validate_names(self, runbook: FileTransferTransformerSchema):
        # Validate container name
        if not re.match(r"^[a-z0-9](?!.*--)[a-z0-9-]{1,61}[a-z0-9]$", runbook.container_name):
            raise LisaException(f"Invalid container name: {runbook.container_name}")
        # Optionally validate blob_directory

    def _ensure_internet(self, node: RemoteNode):
        result = node.execute("ping -c 1 aka.ms", shell=True, no_error_log=True, no_info_log=True)
        if result.exit_code != 0:
            raise LisaException("No internet access on VM. Cannot download AzCopy.")

    def _ensure_required_tools(self, node: RemoteNode):
        for tool in ["wget", "tar", "sudo"]:
            which_result = node.execute(f"which {tool}", shell=True, no_error_log=True, no_info_log=True)
            if which_result.exit_code != 0:
                raise LisaException(f"Required tool '{tool}' is not installed on the VM. Please install it before proceeding.")

    def _ensure_sudo(self, node: RemoteNode):
        sudo_check = node.execute("sudo -n true", shell=True, no_error_log=True, no_info_log=True)
        if sudo_check.exit_code != 0:
            raise LisaException("User does not have passwordless sudo rights. AzCopy installation will fail.")

    def _get_sas_url(self, platform: AzurePlatform, runbook: FileTransferTransformerSchema, writable: bool) -> str:
        from datetime import datetime, timezone
        from urllib.parse import parse_qs, urlparse
        container_name = runbook.container_name
        account_name = runbook.storage_account_name
        # Determine if this is a single file upload
        is_single_file = (
            isinstance(runbook.file_patterns, str)
            and "*" not in runbook.file_patterns
            and runbook.file_patterns != "*"
        )
        # If multiple files and user provided container SAS, use it
        is_multiple_files = (
            runbook.file_patterns is None or
            runbook.file_patterns == "*" or
            isinstance(runbook.file_patterns, list) or
            "*" in (runbook.file_patterns or "")
        )
        if is_multiple_files and getattr(runbook, "container_sas_token", ""):
            self._log.info("Using user-provided container SAS token from runbook for multi-file operation")
            container_client = get_or_create_storage_container(
                credential=platform.credential,
                cloud=platform.cloud,
                account_name=account_name,
                container_name=container_name,
                platform=platform,
            )
            blob_path = runbook.blob_directory.rstrip("/")
            if blob_path:
                sas_url = f"{container_client.url}/{blob_path}?{runbook.container_sas_token}"
            else:
                sas_url = f"{container_client.url}?{runbook.container_sas_token}"
            self._log.info(f"[DEBUG] Using user-provided container SAS URL: {self._mask_sas_url(sas_url)}")
            return sas_url
        # ...existing single-file logic...
        if is_single_file:
            blob_name = f"{runbook.blob_directory}/{runbook.file_patterns}"
        else:
            blob_name = ""  # Will not work for recursive, but keeps current logic
        self._log.info(f"[DEBUG] Preparing to get/create storage container: account={account_name}, container={container_name}")
        try:
            container_client = get_or_create_storage_container(
                credential=platform.credential,
                cloud=platform.cloud,
                account_name=account_name,
                container_name=container_name,
                platform=platform,
            )
        except Exception as ex:
            self._log.error(f"[DEBUG] Failed to get or create storage container: {ex}")
            raise LisaException(f"Failed to get or create storage container: {ex}")
        self._log.info(f"[DEBUG] Generating SAS for blob: {blob_name if blob_name else '[container root]'}")
        self._log.info(f"[DEBUG] System UTC time: {datetime.now(timezone.utc).isoformat()}")
        try:
            sas_token = generate_user_delegation_sas_token(
                container_name=container_name,
                blob_name=blob_name,
                cloud=platform.cloud,
                credential=platform.credential,
                account_name=account_name,
                writable=writable,
                expired_hours=2,
                platform=platform,
            )
        except Exception as ex:
            self._log.error(f"[DEBUG] Failed to generate SAS token: {ex}")
            raise LisaException(f"Failed to generate SAS token: {ex}")
        self._log.info(f"[DEBUG] SAS token (prefix): {sas_token[:50]}...")

        # Parse SAS token for diagnostics
        try:
            qs = parse_qs(sas_token)
            sp = qs.get("sp", [""])[0]
            sr = qs.get("sr", [""])[0]
            self._log.info(f"[DEBUG] SAS permissions (sp): '{sp}'")
            self._log.info(f"[DEBUG] SAS resource type (sr): '{sr}'")
            if "w" not in sp or "c" not in sp:
                self._log.warning(f"[WARN] SAS token missing 'w' (write) or 'c' (create) permissions. sp='{sp}'")
            if sr != "b":
                self._log.warning(f"[WARN] SAS token resource type is not 'b' (blob). sr='{sr}'")
        except Exception as ex:
            self._log.warning(f"[WARN] Could not parse SAS token for diagnostics: {ex}")

        base_url = f"{container_client.url}/{blob_name}".rstrip("/")
        sas_url = f"{base_url}?{sas_token}"
        self._log.info(f"[DEBUG] AzCopy destination URL: {base_url}?<SAS_TOKEN_REDACTED>")
        self._log.info(f"[DEBUG] Full SAS URL: {sas_url[:100]}...<truncated>")
        return sas_url

    def _ensure_directory_exists(self, node: RemoteNode, directory: str, mode: str) -> None:
        check_cmd = f"test -d '{directory}'"
        result = node.execute(check_cmd, shell=True, no_error_log=True, no_info_log=True)
        if result.exit_code != 0:
            if mode == "upload":
                raise LisaException(f"Source directory '{directory}' does not exist on VM.")
            elif mode == "download":
                self._log.info(f"Destination directory '{directory}' does not exist. Creating it with sudo.")
                mkdir_cmd = f"sudo mkdir -p '{directory}'"
                mkdir_result = node.execute(mkdir_cmd, shell=True)
                if mkdir_result.exit_code != 0:
                    raise LisaException(f"Failed to create directory '{directory}' on VM (even with sudo): {mkdir_result.stderr}")
        # Check write permission (with sudo for download mode)
        if mode == "download":
            perm_check = node.execute(f"sudo test -w '{directory}'", shell=True, no_error_log=True, no_info_log=True)
            if perm_check.exit_code != 0:
                self._log.info(f"Attempting to set write permission for '{directory}' with sudo chmod.")
                chmod_result = node.execute(f"sudo chmod a+w '{directory}'", shell=True)
                if chmod_result.exit_code != 0:
                    raise LisaException(f"Failed to set write permission for directory '{directory}' on VM: {chmod_result.stderr}")
        else:
            perm_check = node.execute(f"test -w '{directory}'", shell=True, no_error_log=True, no_info_log=True)
            if perm_check.exit_code != 0:
                self._log.info(f"Attempting to set write permission for '{directory}' with sudo chmod (upload mode).")
                chmod_result = node.execute(f"sudo chmod a+w '{directory}'", shell=True)
                if chmod_result.exit_code != 0:
                    raise LisaException(f"Failed to set write permission for directory '{directory}' on VM (upload mode): {chmod_result.stderr}")
                # Re-check permission after chmod
                perm_check = node.execute(f"test -w '{directory}'", shell=True, no_error_log=True, no_info_log=True)
                if perm_check.exit_code != 0:
                    raise LisaException(f"No write permission for directory '{directory}' on VM after chmod (upload mode).")


    def _ensure_azcopy(self, node: RemoteNode, azcopy_path: str = "azcopy") -> str:
        try:
            result = node.execute(f"{azcopy_path} --version", shell=True, no_error_log=True, no_info_log=True, expected_exit_code=0, timeout=10)
            if result.exit_code == 0:
                self._log.debug("AzCopy is already installed.")
                return azcopy_path
        except Exception as ex:
            self._log.debug(f"AzCopy check failed: {ex}")

        # Install AzCopy
        try:
            install_cmds = [
                "wget -O azcopy.tar.gz https://aka.ms/downloadazcopy-v10-linux",
                "tar -xf azcopy.tar.gz",
                "sudo cp ./azcopy_linux_amd64_*/azcopy /usr/local/bin/",
            ]
            for cmd in install_cmds:
                result = node.execute(cmd, shell=True)
                if result.exit_code != 0:
                    raise LisaException(f"Failed to run '{cmd}': {result.stderr}")
        finally:
            # Always clean up temp files
            node.execute("rm -rf azcopy.tar.gz azcopy_linux_amd64_*", shell=True, no_error_log=True, no_info_log=True)

        # Verify installation and get full path
        which_result = node.execute("which azcopy", shell=True, no_error_log=True, no_info_log=True)
        if which_result.exit_code == 0:
            azcopy_path = which_result.stdout.strip()
        else:
            azcopy_path = "/usr/local/bin/azcopy"
        result = node.execute(f"{azcopy_path} --version", shell=True)
        if result.exit_code != 0:
            raise LisaException("AzCopy installation verification failed.")
        self._log.info("AzCopy installed successfully.")
        return azcopy_path

    def _mask_sas_url(self, url: str) -> str:
        if "?" in url:
            return url.split("?")[0] + "?<SAS_TOKEN_REDACTED>"
        return url

    def _upload_files(self, runbook: FileTransferTransformerSchema, platform: AzurePlatform, node: RemoteNode) -> List[str]:
        sas_url = self._get_sas_url(platform, runbook, writable=True)
        file_patterns = runbook.file_patterns or "*"
        azcopy_path = runbook.azcopy_path or self._ensure_azcopy(node)
        uploaded_urls = []

        self._log.info(f"Uploading from VM directory: {runbook.vm_directory} to blob directory: {runbook.blob_directory}")
        self._log.info(f"AzCopy path: {azcopy_path}")
        self._log.info(f"SAS URL (masked): {self._mask_sas_url(sas_url)}")

        if isinstance(file_patterns, list):
            for pattern in file_patterns:
                src = f"{runbook.vm_directory}/{pattern}"
                self._check_files_exist(node, src)
                azcopy_cmd = f"{azcopy_path} copy '{src}' '{sas_url}' --recursive"
                self._log.info(f"Uploading files with: {self._mask_sas_url(azcopy_cmd)}")
                result = node.execute(azcopy_cmd, shell=True)
                if result.exit_code != 0:
                    self._log.error(f"AzCopy upload failed: {result.stderr}")
                    self._log.error(f"AzCopy stdout: {result.stdout}")
                    raise LisaException(f"AzCopy upload failed for pattern '{pattern}'.")
                uploaded_urls.append(f"{sas_url}/{pattern}")
        else:
            src = f"{runbook.vm_directory}/{file_patterns}"
            self._check_files_exist(node, src)
            azcopy_cmd = f"{azcopy_path} copy '{src}' '{sas_url}' --recursive"
            self._log.info(f"Uploading files with: {self._mask_sas_url(azcopy_cmd)}")
            result = node.execute(azcopy_cmd, shell=True)
            if result.exit_code != 0:
                self._log.error(f"AzCopy upload failed: {result.stderr}")
                self._log.error(f"AzCopy stdout: {result.stdout}")
                raise LisaException(f"AzCopy upload failed for pattern '{file_patterns}'.")
            uploaded_urls.append(sas_url)
        return uploaded_urls

    def _download_files(self, runbook: FileTransferTransformerSchema, platform: AzurePlatform, node: RemoteNode) -> List[str]:
            sas_url = self._get_sas_url(platform, runbook, writable=True)
            file_patterns = runbook.file_patterns or "*"
            azcopy_path = runbook.azcopy_path or self._ensure_azcopy(node)
            downloaded_paths = []

            # For multi-file (wildcard or list), use the SAS URL for the prefix only, insert * before ? for flattening
            if file_patterns == "*" or (isinstance(file_patterns, list) and len(file_patterns) > 1):
                if "?" in sas_url:
                    src = sas_url.replace("?", "/*?")
                else:
                    src = f"{sas_url}/*"
                azcopy_cmd = f"sudo {azcopy_path} copy '{src}' '{runbook.vm_directory}' --recursive"
                self._log.info(f"Downloading files with: {self._mask_sas_url(azcopy_cmd)}")
                result = node.execute(azcopy_cmd, shell=True)
                if result.exit_code != 0:
                    self._log.error(f"AzCopy download failed: {result.stderr}")
                    self._log.error(f"AzCopy stdout: {result.stdout}")
                    raise LisaException(f"AzCopy download failed for pattern '{file_patterns}'.")
                downloaded_paths.append(runbook.vm_directory)
            elif isinstance(file_patterns, list):
                for pattern in file_patterns:
                    src = f"{sas_url}/{pattern}"
                    azcopy_cmd = f"sudo {azcopy_path} copy '{src}' '{runbook.vm_directory}' --recursive"
                    self._log.info(f"Downloading files with: {self._mask_sas_url(azcopy_cmd)}")
                    result = node.execute(azcopy_cmd, shell=True)
                    if result.exit_code != 0:
                        self._log.error(f"AzCopy download failed: {result.stderr}")
                        self._log.error(f"AzCopy stdout: {result.stdout}")
                        raise LisaException(f"AzCopy download failed for pattern '{pattern}'.")
                    downloaded_paths.append(f"{runbook.vm_directory}/{pattern}")
            else:
                # Single file
                src = f"{sas_url}/{file_patterns}"
                azcopy_cmd = f"sudo {azcopy_path} copy '{src}' '{runbook.vm_directory}' --recursive"
                self._log.info(f"Downloading files with: {self._mask_sas_url(azcopy_cmd)}")
                result = node.execute(azcopy_cmd, shell=True)
                if result.exit_code != 0:
                    self._log.error(f"AzCopy download failed: {result.stderr}")
                    self._log.error(f"AzCopy stdout: {result.stdout}")
                    raise LisaException(f"AzCopy download failed for pattern '{file_patterns}'.")
                downloaded_paths.append(f"{runbook.vm_directory}/{file_patterns}")

            # Debug: List blobs in the blob directory before attempting download
            from azure.storage.blob import ContainerClient
            import os
            try:
                # Extract base SAS URL (without file_patterns)
                base_sas_url = sas_url.split("?")[0] + "?" + sas_url.split("?")[1]
                self._log.info(f"[DEBUG] Attempting to list blobs in: {base_sas_url}")
                container_client = ContainerClient.from_container_url(base_sas_url)
                blob_prefix = runbook.file_patterns if isinstance(file_patterns, str) and file_patterns != "*" else ""
                blob_list = list(container_client.list_blobs(name_starts_with=blob_prefix))
                self._log.info(f"[DEBUG] Found {len(blob_list)} blobs in directory '{runbook.blob_directory}' with prefix '{blob_prefix}'")
                for blob in blob_list:
                    self._log.info(f"[DEBUG] Blob: {blob.name}")
            except Exception as ex:
                self._log.warning(f"[DEBUG] Could not list blobs before download: {ex}")
            return downloaded_paths
    
    def _check_files_exist(self, node: RemoteNode, path: str) -> None:
        check_cmd = f"ls {path}"
        result = node.execute(check_cmd, shell=True, no_error_log=True, no_info_log=True)
        if result.exit_code != 0:
            raise LisaException(f"No files found matching '{path}' on VM.")