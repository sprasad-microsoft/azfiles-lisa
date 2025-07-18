from lisa.executable import Tool
from pathlib import PurePath
from typing import List
import re

class Dpkg(Tool):
    @property
    def command(self) -> str:
        return "dpkg"

    def is_valid_package(self, package_path: str) -> bool:
        # Check if the file is a valid deb package
        result = self.run(
            f"--info {package_path}",
            sudo=True,
            shell=True,
            no_error_log=True,
            no_info_log=True,
        )
        return result.exit_code == 0

    def install_local_package(self, package_path: str, force: bool = True) -> None:
        # Install a single deb package
        options = "-i"
        if force:
            options += " --force-all"
        self.run(
            f"{options} {package_path}",
            sudo=True,
            shell=True,
        )

    def install_packages_in_directory(self, directory_path: str, force: bool = True) -> None:
        # Install all .deb packages in the given directory
        options = "-i"
        if force:
            options += " --force-all"
        self.run(
            f"{options} {directory_path}/*.deb",
            sudo=True,
            shell=True,
        )
        # Optionally fix dependencies
        self.node.execute(
            "apt-get -f install -y",
            sudo=True,
            shell=True,
        )

    def validate_all_debs_in_directory(self, directory_path: str) -> List[str]:
        # Returns a list of invalid .deb files (empty if all are valid)
        result = self.node.execute(
            f"ls {directory_path}/*.deb",
            shell=True,
            sudo=False,
        )
        deb_files = result.stdout.strip().splitlines()
        invalid_files = []
        for deb in deb_files:
            if not self.is_valid_package(deb):
                invalid_files.append(deb)
        return invalid_files

    def is_kernel_package(self, package_name: str) -> bool:
        """
        Check if a .deb package is a kernel-related package.
        Returns True for packages like linux-image-*.deb, linux-modules-*.deb
        """
        # Remove path and .deb extension to get just the package name
        base_name = package_name.split('/')[-1]
        if base_name.endswith('.deb'):
            base_name = base_name[:-4]
        
        # Check for kernel image packages (but not headers, debug, or other kernel packages)
        kernel_patterns = [
            r'^linux-image-[0-9]',  # linux-image-6.5.0-rc1+
            r'^linux-image-.*-azure',  # linux-image-5.15.0-1019-azure
            r'^linux-image-.*-generic',  # linux-image-5.15.0-generic
            r'^linux-image-.*-lowlatency',  # linux-image-5.15.0-lowlatency
            r'^linux-libc-dev',  # linux-libc-dev (userspace kernel headers)
            r'^linux-modules-[0-9]',  # linux-modules-6.5.0-rc1+
            r'^linux-modules-.*-azure',  # linux-modules-5.15.0-1019-azure
            r'^linux-modules-.*-generic',  # linux-modules-5.15.0-generic
        ]
        
        # Exclude non-image packages
        exclude_patterns = [
            r'linux-.*-headers',
            r'linux-.*-tools',
            r'linux-.*-dbg',
            r'linux-.*-dev',
            r'linux-.*-doc',
        ]
        
        # Check exclusions first
        for pattern in exclude_patterns:
            if re.match(pattern, base_name, re.IGNORECASE):
                return False
        
        # Check for kernel image patterns
        for pattern in kernel_patterns:
            if re.match(pattern, base_name, re.IGNORECASE):
                return True
        
        return False

    def is_bootable_kernel_package(self, package_name: str) -> bool:
        """
        Check if a .deb package is specifically a bootable kernel image.
        Only these packages should trigger GRUB updates.
        """
        # Remove path and .deb extension to get just the package name
        base_name = package_name.split('/')[-1]
        if base_name.endswith('.deb'):
            base_name = base_name[:-4]
        
        # Only linux-image packages affect the boot kernel
        bootable_kernel_patterns = [
            r'^linux-image-[0-9]',  # linux-image-6.5.0-rc1+
            r'^linux-image-.*-azure',  # linux-image-5.15.0-1019-azure
            r'^linux-image-.*-generic',  # linux-image-5.15.0-generic
            r'^linux-image-.*-lowlatency',  # linux-image-5.15.0-lowlatency
        ]
        
        for pattern in bootable_kernel_patterns:
            if re.match(pattern, base_name, re.IGNORECASE):
                return True
        
        return False

    def extract_kernel_version_from_package(self, package_name: str) -> str:
        """
        Extract kernel version from package name.
        Example: linux-image-6.5.0-rc1+_6.5.0~rc1-12_amd64.deb -> 6.5.0-rc1+
        Note: linux-libc-dev packages typically don't contain version info in the name
        """
        base_name = package_name.split('/')[-1]
        if base_name.endswith('.deb'):
            base_name = base_name[:-4]
        
        # Skip version extraction for linux-libc-dev as it doesn't follow the same naming
        if base_name.startswith('linux-libc-dev'):
            return ""
        
        # Pattern to extract version from linux-image-VERSION
        # Handle various formats including RC kernels
        version_patterns = [
            # RC kernels: linux-image-6.16.0-rc4+ -> 6.16.0-rc4+
            r'^linux-image-([0-9]+\.[0-9]+\.[0-9]+-rc[0-9]+[^_]*)',
            # Standard kernels: linux-image-6.5.0-rc1+ -> 6.5.0-rc1+  
            r'^linux-image-([0-9]+\.[0-9]+\.[0-9]+[^_]*)',
            # Azure/distro kernels: linux-image-5.15.0-1019-azure -> 5.15.0-1019-azure
            r'^linux-image-([0-9]+\.[0-9]+\.[0-9]+-[^_]+)',
            # Generic fallback
            r'^linux-image-([^_]+)',
        ]
        
        for pattern in version_patterns:
            match = re.match(pattern, base_name, re.IGNORECASE)
            if match:
                version = match.group(1)
                return version
        
        return ""