# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
from pathlib import PurePosixPath
from typing import Any, Dict, List, Type

from lisa import schema
from lisa.operating_system import Debian, Posix
from lisa.tools import Cat, Dpkg, Uname
from lisa.transformers.package_installer import PackageInstaller, PackageInstallerSchema
from lisa.util import UnsupportedDistroException
from typing import cast
import re


class DEBPackageInstallerTransformer(PackageInstaller):
    @classmethod
    def type_name(cls) -> str:
        return "deb_package_installer"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return PackageInstallerSchema

    @property
    def _output_names(self) -> List[str]:
        return []

    def _validate(self) -> None:
        if not isinstance(self._node.os, Debian):
            raise UnsupportedDistroException(
                self._node.os,
                f"'{self.type_name()}' transformer only supports Debian-based Distros.",
            )
        runbook: PackageInstallerSchema = self.runbook
        if runbook.files == ["*"]:
            # Validate all .deb files in the directory
            directory = PurePosixPath(runbook.directory)
            self._log.debug(f"Validating all .deb files in {directory}")
            self._node.tools[Dpkg].validate_all_debs_in_directory(str(directory))
        else:
            super()._validate()

    def _validate_package(self, file: str) -> None:
        assert self._node.tools[Dpkg].is_valid_package(
            file
        ), f"Provided file {file} is not a deb"

    def _install_package(self, file: str) -> None:
        self._node.tools[Dpkg].install_local_package(file, force=True)

    def _internal_run(self) -> Dict[str, Any]:
        runbook: PackageInstallerSchema = self.runbook
        directory = PurePosixPath(runbook.directory)
        uname = self._node.tools[Uname]

        # Log kernel version before installation
        kernel_before = uname.get_linux_information().kernel_version_raw
        self._log.info(f"Kernel version before installation: {kernel_before}")

        # List and log all files in the directory
        files_in_dir = self._node.execute(f"ls -l {directory}", shell=True).stdout
        self._log.info(f"Contents of {directory} before installation:\n{files_in_dir}")

        # Track if kernel packages were installed for GRUB update
        kernel_packages_installed = []
        installed_kernel_version = None

        if runbook.files == ["*"]:
            # Find .deb files in the directory
            deb_files = [
                line.split()[-1]
                for line in files_in_dir.splitlines()
                if line.strip().endswith(".deb")
            ]
            if not deb_files:
                self._log.warning(f"No .deb files found in {directory}. Skipping installation.")
                return {}
            
            # Check if any .deb files are kernel packages
            dpkg = self._node.tools[Dpkg]
            for deb_file in deb_files:
                if dpkg.is_kernel_package(deb_file):
                    kernel_packages_installed.append(deb_file)
                    # Only extract version from bootable kernel images
                    if dpkg.is_bootable_kernel_package(deb_file):
                        version = dpkg.extract_kernel_version_from_package(deb_file)
                        if version and not installed_kernel_version:
                            installed_kernel_version = version
                            self._log.info(f"Detected bootable kernel image: {deb_file} (version: {version})")
            
            # Install all .deb files in the directory
            self._log.info(f"Installing all .deb packages in {directory}")
            self._node.tools[Dpkg].install_packages_in_directory(str(directory))
        else:
            self._log.info(f"Installing packages: {runbook.files}")
            success = []
            failed = []
            for file in runbook.files:
                full_path = self._node.get_str_path(directory.joinpath(file))
                # Check if file exists on remote node
                result = self._node.execute(f"test -f {full_path}", shell=True, no_error_log=True)
                if result.exit_code != 0:
                    self._log.error(f"File not found: {full_path}. Skipping.")
                    failed.append(file)
                    continue
                try:
                    # Check if this is a kernel package before installing
                    dpkg = self._node.tools[Dpkg]
                    if dpkg.is_kernel_package(file):
                        kernel_packages_installed.append(file)
                        # Only extract version from bootable kernel images
                        if dpkg.is_bootable_kernel_package(file):
                            version = dpkg.extract_kernel_version_from_package(file)
                            if version and not installed_kernel_version:
                                installed_kernel_version = version
                                self._log.info(f"Detected bootable kernel image: {file} (version: {version})")
                    
                    self._install_package(full_path)
                    success.append(file)
                except Exception as e:
                    self._log.error(f"Failed to install {full_path}: {e}")
                    failed.append(file)
            self._log.info(f"Successfully installed: {success}")
            if failed:
                self._log.warning(f"Failed to install: {failed}")

        # Update GRUB if bootable kernel packages were installed
        if kernel_packages_installed and installed_kernel_version:
            self._log.info(f"Kernel packages detected: {kernel_packages_installed}")
            self._log.info(f"Bootable kernel version found: {installed_kernel_version}")
            self._update_grub_for_kernel(installed_kernel_version)
        elif kernel_packages_installed:
            self._log.info(f"Kernel-related packages detected: {kernel_packages_installed}")
            self._log.info("No bootable kernel image found, skipping GRUB update")
        else:
            self._log.debug("No kernel packages detected")

        if runbook.reboot:
            self._log.info("Rebooting node after package installation.")
            try:
                self._node.reboot(time_out=900)
                
                # After reboot, immediately check what GRUB is set to
                try:
                    grub_env_result = self._node.execute("grub-editenv list", sudo=True, no_error_log=True)
                    if grub_env_result.exit_code == 0:
                        self._log.info(f"GRUB environment after reboot: {grub_env_result.stdout}")
                    
                    # Check /etc/default/grub to see if our setting persisted
                    cat = self._node.tools[Cat]
                    grub_default_result = cat.run("/etc/default/grub", no_error_log=True)
                    if grub_default_result.exit_code == 0:
                        grub_default_lines = [line for line in grub_default_result.stdout.split('\n') if 'GRUB_DEFAULT' in line]
                        for line in grub_default_lines:
                            self._log.info(f"GRUB_DEFAULT setting after reboot: {line.strip()}")
                    
                    # Check what kernel actually booted
                    cmdline_result = cat.run("/proc/cmdline", no_error_log=True)
                    if cmdline_result.exit_code == 0:
                        self._log.info(f"Kernel command line after reboot: {cmdline_result.stdout.strip()}")
                        
                except Exception as debug_e:
                    self._log.warning(f"Could not check post-reboot GRUB state: {debug_e}")
                    
            except Exception as e:
                self._log.error(f"Reboot failed: {e}")

        # Log kernel version after installation/reboot
        kernel_after = uname.get_linux_information().kernel_version_raw
        self._log.info(f"Kernel version after installation (uname): {kernel_after}")
        
        # Also check /proc/cmdline for authoritative boot information
        try:
            cat = self._node.tools[Cat]
            cmdline_result = cat.run("/proc/cmdline", no_error_log=True)
            if cmdline_result.exit_code == 0:
                cmdline = cmdline_result.stdout.strip()
                # Extract kernel version from BOOT_IMAGE
                import re
                boot_image_match = re.search(r'BOOT_IMAGE=/boot/vmlinuz-([^\s]+)', cmdline)
                if boot_image_match:
                    actual_booted_kernel = boot_image_match.group(1)
                    self._log.info(f"Kernel version after installation (cmdline): {actual_booted_kernel}")
                    
                    # Use cmdline as authoritative source if available
                    if actual_booted_kernel != kernel_after:
                        self._log.warning(f"Kernel version mismatch between uname ({kernel_after}) and cmdline ({actual_booted_kernel})")
                        self._log.info(f"Using cmdline as authoritative source: {actual_booted_kernel}")
                        kernel_after = actual_booted_kernel
                else:
                    self._log.debug("Could not extract kernel version from /proc/cmdline")
        except Exception as e:
            self._log.debug(f"Could not read /proc/cmdline: {e}")

        # Log kernel version change with corrected detection
        if kernel_before != kernel_after:
            self._log.info(f"Kernel version changed: {kernel_before} -> {kernel_after}")
        else:
            # Check if we're installing the same version that's already running
            if kernel_packages_installed and installed_kernel_version:
                if kernel_after == installed_kernel_version:
                    self._log.info(f"Kernel version confirmed: Successfully running the intended kernel {kernel_after}")
                else:
                    self._log.warning(f"Kernel version mismatch: Expected {installed_kernel_version}, got {kernel_after}")
            else:
                self._log.info(f"Kernel version unchanged: {kernel_after}")
            
            # If kernel didn't change after installation, log GRUB default for debugging
            if kernel_packages_installed and kernel_after != installed_kernel_version:
                self._log.warning("Expected kernel change but version remained the same")
                try:
                    # Check what GRUB is set to boot by default
                    grub_default = self._node.execute("grub-editenv list", sudo=True, no_error_log=True)
                    self._log.info(f"GRUB environment: {grub_default.stdout}")
                    
                    # Check if the new kernel is available in GRUB menu
                    cat = self._node.tools[Cat]
                    grub_result = cat.run("/boot/grub/grub.cfg", sudo=True, no_error_log=True)
                    if grub_result.exit_code == 0:
                        # Count total menuentry options
                        menuentry_count = grub_result.stdout.count('menuentry ')
                        self._log.info(f"Total GRUB menu entries found: {menuentry_count}")
                        
                        # Check if our kernel appears in submenu vs main menu
                        main_entries = []
                        submenu_entries = []
                        lines = grub_result.stdout.split('\n')
                        in_submenu = False
                        
                        for line in lines:
                            if 'submenu ' in line:
                                in_submenu = True
                            elif 'menuentry ' in line and 'Linux' in line:
                                if in_submenu:
                                    submenu_entries.append(line.strip())
                                else:
                                    main_entries.append(line.strip())
                        
                        self._log.info(f"Main menu entries: {len(main_entries)}")
                        self._log.info(f"Submenu entries: {len(submenu_entries)}")
                        
                        # Show first few entries for debugging
                        for i, entry in enumerate(main_entries[:3]):
                            self._log.info(f"Main entry {i}: {entry}")
                        for i, entry in enumerate(submenu_entries[:3]):
                            self._log.info(f"Submenu entry {i}: {entry}")
                            
                except Exception as e:
                    self._log.warning(f"Could not check GRUB configuration: {e}")

        return {}

    def _update_grub_for_kernel(self, kernel_version: str) -> None:
        """
        Update GRUB configuration to set the new kernel as default.
        Uses LISA's built-in GRUB functionality.
        """
        try:
            self._log.info(f"Updating GRUB configuration for new kernel: {kernel_version}")
            
            # Use LISA's built-in GRUB functionality
            posix_os = cast(Posix, self._node.os)
            
            if kernel_version:
                # Set the new kernel as the default boot option
                posix_os.replace_boot_kernel(kernel_version)
                self._log.info("GRUB configuration updated successfully")
            else:
                # Fallback: regenerate GRUB config without setting specific kernel
                self._log.warning("No specific kernel version provided, regenerating GRUB config")
                grub = self._node.os.install_grub()
                grub.update_grub_conf()
                self._log.info("GRUB configuration regenerated successfully")
                
        except Exception as e:
            self._log.error(f"Failed to update GRUB for kernel '{kernel_version}': {e}")
            
            # Try a fallback approach: just regenerate GRUB config
            try:
                self._log.info("Attempting fallback: regenerating GRUB configuration")
                grub = self._node.os.install_grub()
                grub.update_grub_conf()
                self._log.info("Fallback GRUB regeneration succeeded")
            except Exception as fallback_e:
                self._log.error(f"Fallback GRUB update also failed: {fallback_e}")
                # Don't fail the entire transformer, just log the error
                self._log.warning("GRUB update failed, but continuing with transformation. "
                                "The new kernel may still be available after reboot via package's own GRUB configuration.")