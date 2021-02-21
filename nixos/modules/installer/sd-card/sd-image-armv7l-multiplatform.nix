# To build, use:
# nix-build nixos -I nixos-config=nixos/modules/installer/sd-card/sd-image-armv7l-multiplatform.nix -A config.system.build.sdImage
{ config, lib, pkgs, ... }:

with lib;

let
  rootfsImage = pkgs.callPackage ../../../lib/make-ext4-fs.nix ({
    inherit (config.sdImage) storePaths;
    compressImage = true;
    populateImageCommands = config.sdImage.populateRootCommands;
    volumeLabel = "NIXOS_SD";
  } // optionalAttrs (config.sdImage.rootPartitionUUID != null) {
    uuid = config.sdImage.rootPartitionUUID;
  });
in
{
  options.sdImage = {
    imageName = mkOption {
      default = "${config.sdImage.imageBaseName}-${config.system.nixos.label}-${pkgs.stdenv.hostPlatform.system}.img";
      description = ''
        Name of the generated image file.
      '';
    };

    imageBaseName = mkOption {
      default = "nixos-sd-image";
      description = ''
        Prefix of the name of the generated image file.
      '';
    };

    storePaths = mkOption {
      type = with types; listOf package;
      example = literalExample "[ pkgs.stdenv ]";
      description = ''
        Derivations to be included in the Nix store in the generated SD image.
      '';
    };

    firmwarePartitionID = mkOption {
      type = types.str;
      default = "0x2178694e";
      description = ''
        Volume ID for the /boot/firmware partition on the SD card. This value
        must be a 32-bit hexadecimal number.
      '';
    };

    firmwarePartitionName = mkOption {
      type = types.str;
      default = "FIRMWARE";
      description = ''
        Name of the filesystem which holds the boot firmware.
      '';
    };

    rootPartitionUUID = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "14e19a7b-0ae0-484d-9d54-43bd6fdc20c7";
      description = ''
        UUID for the filesystem on the main NixOS partition on the SD card.
      '';
    };

    firmwareSize = mkOption {
      type = types.int;
      # As of 2019-08-18 the Raspberry pi firmware + u-boot takes ~18MiB
      default = 30;
      description = ''
        Size of the /boot/firmware partition, in megabytes.
      '';
    };

    populateFirmwareCommands = mkOption {
      example = literalExample "'' cp \${pkgs.myBootLoader}/u-boot.bin firmware/ ''";
      description = ''
        Shell commands to populate the ./firmware directory.
        All files in that directory are copied to the
        /boot/firmware partition on the SD image.
      '';
    };

    populateRootCommands = mkOption {
      example = literalExample "''\${config.boot.loader.generic-extlinux-compatible.populateCmd} -c \${config.system.build.toplevel} -d ./files/boot''";
      description = ''
        Shell commands to populate the ./files directory.
        All files in that directory are copied to the
        root (/) partition on the SD image. Use this to
        populate the ./files/boot (/boot) directory.
      '';
    };

    postBuildCommands = mkOption {
      example = literalExample "'' dd if=\${pkgs.myBootLoader}/SPL of=$img bs=1024 seek=1 conv=notrunc ''";
      default = "";
      description = ''
        Shell commands to run after the image is built.
        Can be used for boards requiring to dd u-boot SPL before actual partitions.
      '';
    };

    compressImage = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Whether the SD image should be compressed using
        <command>zstd</command>.
      '';
    };

  };

  config = {
    fileSystems = {
      "/boot/firmware" = {
        device = "/dev/disk/by-label/${config.sdImage.firmwarePartitionName}";
        fsType = "vfat";
        # Alternatively, this could be removed from the configuration.
        # The filesystem is not needed at runtime, it could be treated
        # as an opaque blob instead of a discrete FAT32 filesystem.
        options = [ "nofail" "noauto" ];
      };
      "/" = {
        device = "/dev/disk/by-label/NIXOS_SD";
        fsType = "ext4";
      };
    };

    sdImage.storePaths = [ config.system.build.toplevel ];

    system.build.sdImage = pkgs.callPackage ({ stdenv, dosfstools, e2fsprogs,
    mtools, libfaketime, util-linux, zstd }: stdenv.mkDerivation {
      name = config.sdImage.imageName;

      nativeBuildInputs = [ dosfstools e2fsprogs mtools libfaketime util-linux zstd ];

      inherit (config.sdImage) compressImage;

      buildCommand = ''
        mkdir -p $out/nix-support $out/sd-image
        export img=$out/sd-image/${config.sdImage.imageName}

        echo "${pkgs.stdenv.buildPlatform.system}" > $out/nix-support/system
        if test -n "$compressImage"; then
          echo "file sd-image $img.zst" >> $out/nix-support/hydra-build-products
        else
          echo "file sd-image $img" >> $out/nix-support/hydra-build-products
        fi

        echo "Decompressing rootfs image"
        zstd -d --no-progress "${rootfsImage}" -o ./root-fs.img

        # Gap in front of the first partition, in MiB
        gap=8

        # Create the image file sized to fit /boot/firmware and /, plus slack for the gap.
        rootSizeBlocks=$(du -B 512 --apparent-size ./root-fs.img | awk '{ print $1 }')
        firmwareSizeBlocks=$((${toString config.sdImage.firmwareSize} * 1024 * 1024 / 512))
        imageSize=$((rootSizeBlocks * 512 + firmwareSizeBlocks * 512 + gap * 1024 * 1024))
        truncate -s $imageSize $img

        # type=b is 'W95 FAT32', type=83 is 'Linux'.
        # The "bootable" partition is where u-boot will look file for the bootloader
        # information (dtbs, extlinux.conf file).
        sfdisk $img <<EOF
            label: dos
            label-id: ${config.sdImage.firmwarePartitionID}

            start=''${gap}M, size=$firmwareSizeBlocks, type=b
            start=$((gap + ${toString config.sdImage.firmwareSize}))M, type=83, bootable
        EOF

        # Copy the rootfs into the SD image
        eval $(partx $img -o START,SECTORS --nr 2 --pairs)
        dd conv=notrunc if=./root-fs.img of=$img seek=$START count=$SECTORS

        # Create a FAT32 /boot/firmware partition of suitable size into firmware_part.img
        eval $(partx $img -o START,SECTORS --nr 1 --pairs)
        truncate -s $((SECTORS * 512)) firmware_part.img
        faketime "1970-01-01 00:00:00" mkfs.vfat -i ${config.sdImage.firmwarePartitionID} -n ${config.sdImage.firmwarePartitionName} firmware_part.img

        # Populate the files intended for /boot/firmware
        mkdir firmware
        ${config.sdImage.populateFirmwareCommands}

        # Copy the populated /boot/firmware into the SD image
        (cd firmware; mcopy -psvm -i ../firmware_part.img ./* ::)
        # Verify the FAT partition before copying it.
        fsck.vfat -vn firmware_part.img
        dd conv=notrunc if=firmware_part.img of=$img seek=$START count=$SECTORS

        ${config.sdImage.postBuildCommands}

        if test -n "$compressImage"; then
            zstd -T$NIX_BUILD_CORES --rm $img
        fi
      '';
    }) {};

    boot.postBootCommands = ''
      # On the first boot do some maintenance tasks
      if [ -f /nix-path-registration ]; then
        set -euo pipefail
        set -x
        # Figure out device names for the boot device and root filesystem.
        rootPart=$(${pkgs.util-linux}/bin/findmnt -n -o SOURCE /)
        bootDevice=$(lsblk -npo PKNAME $rootPart)
        partNum=$(lsblk -npo MAJ:MIN $rootPart | ${pkgs.gawk}/bin/awk -F: '{print $2}')

        # Resize the root partition and the filesystem to fit the disk
        echo ",+," | sfdisk -N$partNum --no-reread $bootDevice
        ${pkgs.parted}/bin/partprobe
        ${pkgs.e2fsprogs}/bin/resize2fs $rootPart

        # Register the contents of the initial Nix store
        ${config.nix.package.out}/bin/nix-store --load-db < /nix-path-registration

        # nixos-rebuild also requires a "system" profile and an /etc/NIXOS tag.
        touch /etc/NIXOS
        ${config.nix.package.out}/bin/nix-env -p /nix/var/nix/profiles/system --set /run/current-system

        # Prevents this from running on later boots.
        rm -f /nix-path-registration
      fi
    '';
    boot.loader.grub.enable = false;
    boot.loader.generic-extlinux-compatible.enable = true;
  
    boot.consoleLogLevel = lib.mkDefault 7;
    boot.kernelPackages = pkgs.linuxPackages_latest;
    # The serial ports listed here are:
    # - ttyS0: for Tegra (Jetson TK1)
    # - ttymxc0: for i.MX6 (Wandboard)
    # - ttyAMA0: for Allwinner (pcDuino3 Nano) and QEMU's -machine virt
    # - ttyO0: for OMAP (BeagleBone Black)
    # - ttySAC2: for Exynos (ODROID-XU3)
    boot.kernelParams = ["console=ttyS0,115200n8" "console=ttymxc0,115200n8" "console=ttyAMA0,115200n8" "console=ttyO0,115200n8" "console=ttySAC2,115200n8" "console=tty0"];
  
    sdImage = {
      populateFirmwareCommands = let
        configTxt = pkgs.writeText "config.txt" ''
          # Prevent the firmware from smashing the framebuffer setup done by the mainline kernel
          # when attempting to show low-voltage or overtemperature warnings.
          avoid_warnings=1
  
          [pi2]
          kernel=u-boot-rpi2.bin
  
          [pi3]
          kernel=u-boot-rpi3.bin
  
          # U-Boot used to need this to work, regardless of whether UART is actually used or not.
          # TODO: check when/if this can be removed.
          enable_uart=1
        '';
        in ''
          (cd ${pkgs.raspberrypifw}/share/raspberrypi/boot && cp bootcode.bin fixup*.dat start*.elf $NIX_BUILD_TOP/firmware/)
          cp ${pkgs.ubootRaspberryPi2}/u-boot.bin firmware/u-boot-rpi2.bin
          cp ${pkgs.ubootRaspberryPi3_32bit}/u-boot.bin firmware/u-boot-rpi3.bin
          cp ${configTxt} firmware/config.txt
        '';
      populateRootCommands = ''
        mkdir -p ./files/boot
        ${config.boot.loader.generic-extlinux-compatible.populateCmd} -c ${config.system.build.toplevel} -d ./files/boot
      '';
    };
  };

  imports = [
    ../../profiles/base.nix
  ];

}
