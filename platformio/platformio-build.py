import hashlib
import sys
import warnings
import shutil
import os

from SCons.Script import COMMAND_LINE_TARGETS, Builder, DefaultEnvironment

from platformio import fs
from platformio import util
from platformio.builder.tools.piolib import PlatformIOLibBuilder
from platformio.compat import WINDOWS, hashlib_encode_data

env = DefaultEnvironment()
platform = env.PioPlatform()
board = env.BoardConfig()

FRAMEWORK_DIR = platform.get_package_dir("mbed5")
assert os.path.isdir(FRAMEWORK_DIR)

# Be sure that the packages and tools paths are in the search path
warnings.simplefilter("ignore")
sys.path.insert(
    0,
    os.path.join(
        FRAMEWORK_DIR, "platformio", "package_deps", "py%d" % sys.version_info.major
    ),
)
sys.path.insert(1, FRAMEWORK_DIR)

from pio_mbed_adapter import PlatformioMbedAdapter


# Long paths Windows hook
if WINDOWS:
    from ctypes import create_unicode_buffer, windll, wintypes

    _GetShortPathNameW = windll.kernel32.GetShortPathNameW
    _GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _GetShortPathNameW.restype = wintypes.DWORD


def shorten_path(path):
    if not WINDOWS:
        return path
    output_buf_size = 0
    while True:
        output_buf = create_unicode_buffer(output_buf_size)
        needed = _GetShortPathNameW(path, output_buf, output_buf_size)
        if output_buf_size >= needed:
            return output_buf.value
        else:
            output_buf_size = needed


def get_dynamic_manifest(lib_path):
    def _fix_paths(paths, lib_path):
        result = []
        for p in paths:
            fixed_path = p.replace(
                os.path.join("features", "unsupported", os.path.basename(lib_path), ""),
                "",
            )
            result.append(fixed_path)
        return result

    lib_processor = PlatformioMbedAdapter(
        [lib_path],
        env.subst("$PROJECTSRC_DIR"),
        get_mbed_target(env.subst("$BOARD")),
        FRAMEWORK_DIR,
    )

    config = lib_processor.extract_project_info(generate_config=False)
    src_files = _fix_paths(config.get("src_files"), lib_path)

    inc_dirs = [
        os.path.join(FRAMEWORK_DIR, d).replace("\\", "/")
        for d in config.get("inc_dirs")
        if not os.path.isabs(d)
    ]

    name = os.path.basename(lib_path)

    manifest = {
        "name": "mbed-" + name,
        "build": {"flags": ["-I."], "srcFilter": ["-<*>"], "libArchive": False},
    }

    if inc_dirs:
        extra_script = os.path.join(env.subst("$BUILD_DIR"), name + "_extra_script.py")
        manifest["build"]["extraScript"] = extra_script.replace("\\", "/")
        if not os.path.isfile(extra_script):
            with open(extra_script, "w") as fp:
                fp.write("Import('env')\n")
                fp.write(
                    "env.Prepend(CPPPATH=[%s])" % ("'" + "', '".join(inc_dirs) + "'")
                )

    for f in src_files:
        manifest["build"]["srcFilter"].extend([" +<%s>" % f])

    return manifest


def get_mbed_target(board_type):
    variants_remap = util.load_json(
        os.path.join(FRAMEWORK_DIR, "platformio", "variants_remap.json")
    )
    variant = (
        variants_remap[board_type]
        if board_type in variants_remap
        else board_type.upper()
    )
    return board.get("build.mbed_variant", variant)


def get_build_profile(cpp_defines):
    if "MBED_BUILD_PROFILE_RELEASE" in cpp_defines:
        return "release"
    elif "MBED_BUILD_PROFILE_DEBUG" in cpp_defines:
        return "debug"
    else:
        return "develop"


def _file_long_data(env, data):
    tmp_file = os.path.join(
        "$BUILD_DIR", "longinc-%s" % hashlib.md5(hashlib_encode_data(data)).hexdigest()
    )
    build_dir = env.subst("$BUILD_DIR")
    if not os.path.isdir(build_dir):
        os.makedirs(build_dir)
    if os.path.isfile(env.subst(tmp_file)):
        return tmp_file
    with open(env.subst(tmp_file), "w") as fp:
        fp.write(data)
    return tmp_file


def long_incflags_hook(incflags):
    return '@"%s"' % _file_long_data(
        env, "\n".join(fs.to_unix_path(f) if WINDOWS else f for f in incflags)
    )


def get_inc_flags():
    inc_paths = [
        os.path.join(FRAMEWORK_DIR, d)
        for d in configuration.get("inc_dirs")
        if not os.path.isabs(d)
    ]

    if "idedata" in COMMAND_LINE_TARGETS:
        return {"CPPPATH": inc_paths}

    # Framework adds a great number of include paths which requires
    # significant amount of time for SCons to scan for changes in CPPPATH.
    # Since files in framework most likely won't change, the
    # paths are added directly to CCFLAGS with "-iwithprefixbefore" flag to
    # reduce the length of compile command

    framework_path = shorten_path(FRAMEWORK_DIR) if WINDOWS else FRAMEWORK_DIR
    if WINDOWS:
        inc_paths = [shorten_path(p) for p in inc_paths]
    flags = ["-iprefix", framework_path] + [
        long_incflags_hook(
            [
                "-iwithprefixbefore" + inc.replace(framework_path, "")
                for inc in inc_paths
            ]
        )
    ]

    return {"CCFLAGS": flags}


#
# Print warnings about deprecated flags
#


cpp_defines = env.Flatten(env.get("CPPDEFINES", []))
for f in ("PIO_FRAMEWORK_MBED_FILESYSTEM_PRESENT", "PIO_FRAMEWORK_MBED_EVENTS_PRESENT"):
    if f in cpp_defines:
        print(
            "Warning! %s option "
            "is now obsolete. Please use mbed_app.json configuration file "
            "and/or a standalone library!" % f
        )

src_paths = [
    os.path.join(FRAMEWORK_DIR, "drivers"),
    os.path.join(FRAMEWORK_DIR, "events"),
    os.path.join(FRAMEWORK_DIR, "hal"),
    os.path.join(FRAMEWORK_DIR, "platform"),
    os.path.join(FRAMEWORK_DIR, "targets"),
]

MBED_RTOS = "PIO_FRAMEWORK_MBED_RTOS_PRESENT" in env.Flatten(env.get("CPPDEFINES", []))

if MBED_RTOS:
    src_paths.extend(
        [
            os.path.join(FRAMEWORK_DIR, "cmsis"),
            os.path.join(FRAMEWORK_DIR, "components"),
            os.path.join(FRAMEWORK_DIR, "features"),
            os.path.join(FRAMEWORK_DIR, "rtos"),
        ]
    )

else:
    # in mbed 2 only cmsis headers used
    env.Append(CPPPATH=[os.path.join(FRAMEWORK_DIR, "cmsis", "TARGET_CORTEX_M")])


if not os.path.isdir(env.subst("$BUILD_DIR")):
    os.makedirs(env.subst("$BUILD_DIR"))

app_config = os.path.join(env.subst("$PROJECT_DIR"), "mbed_app.json")
if not os.path.isfile(app_config):
    app_config = None

build_profile = get_build_profile(cpp_defines)

framework_processor = PlatformioMbedAdapter(
    src_paths,
    env.subst("$BUILD_DIR"),
    get_mbed_target(env.subst("$BOARD")),
    FRAMEWORK_DIR,
    app_config,
    build_profile,
    env.subst("$PROJECT_DIR"),
)

try:
    print("Collecting mbed sources...")
    configuration = framework_processor.extract_project_info(generate_config=True)
except Exception as exc:
    sys.stderr.write("mbed build API internal error\n")
    print(exc)
    env.Exit(1)

env.Replace(AS="$CC", ASCOM="$ASPPCOM")

for scope in ("asm", "c", "cxx"):
    if "-c" in configuration.get("build_flags").get(scope, []):
        configuration.get("build_flags").get(scope).remove("-c")


env.AppendUnique(
    ASFLAGS=configuration.get("build_flags").get("asm"),
    CFLAGS=configuration.get("build_flags").get("c"),
    CCFLAGS=["-includembed_config.h"] + configuration.get("build_flags").get("common"),
    CPPDEFINES=configuration.get("build_symbols"),
    CPPPATH=[FRAMEWORK_DIR, "$BUILD_DIR", "$PROJECTSRC_DIR"],
    CXXFLAGS=configuration.get("build_flags").get("cxx"),
    LINKFLAGS=configuration.get("build_flags").get("ld"),
    LIBS=configuration.get("libs") + configuration.get("syslibs"),
)

# Note: this line should be called before appending CCFLAGS to ASFLAGS
env.Append(**get_inc_flags())

env.Append(
    ASFLAGS=env.get("CCFLAGS", [])[:],
    LIBPATH=[
        p if os.path.isabs(p) else os.path.join(FRAMEWORK_DIR, p)
        for p in configuration.get("lib_paths")
    ],
    LIBS=["c", "gcc"],  # Fixes linker issues in some cases
)

if "nordicnrf5" in env.get("PIOPLATFORM"):
    has_soft_device = len(configuration.get("hex")) > 0
    if has_soft_device:
        softdevice_hex_path = os.path.join(FRAMEWORK_DIR, configuration.get("hex")[0])
        if os.path.isfile(softdevice_hex_path):
            env.Append(SOFTDEVICEHEX=softdevice_hex_path)
        else:
            print(
                "Warning! Cannot find softdevice binary"
                "Firmware will be linked without it!"
            )

#
# Linker requires preprocessing with link flags
#

if not board.get("build.ldscript", ""):
    ldscript = os.path.join(FRAMEWORK_DIR, configuration.get("ldscript", [])[0] or "")
    if board.get("build.mbed.ldscript", ""):
        ldscript = env.subst(board.get("build.mbed.ldscript"))
    if os.path.isfile(ldscript):
        linker_script = env.Command(
            os.path.join(
                "$BUILD_DIR", "%s.link_script.ld" % os.path.basename(ldscript)
            ),
            ldscript,
            env.VerboseAction(
                "%s -E -P $LINKFLAGS $SOURCE -o $TARGET"
                % env.subst("$GDB").replace("-gdb", "-cpp"),
                "Generating LD script $TARGET",
            ),
        )

        env.Depends("$BUILD_DIR/$PROGNAME$PROGSUFFIX", linker_script)
        env.Replace(LDSCRIPT_PATH=linker_script)
    else:
        print("Warning! Couldn't find linker script file!")

#
# Compile core part
#

src_filter = "-<*>"
usb_dir = os.path.join("drivers", "source", "usb")
for f in configuration.get("src_files"):
    # Exclude USB related source files from mbed2 build as they contain
    # references to RTOS API which is also not included.
    if not MBED_RTOS and usb_dir in f:
        continue
    src_filter = src_filter + " +<%s>" % f

env.BuildSources(
    os.path.join("$BUILD_DIR", "FrameworkMbed"), FRAMEWORK_DIR, src_filter=src_filter
)

#
# mbed has its own independent merge process
#


def merge_firmwares(target, source, env):
    framework_processor.merge_apps(env.subst(source)[0], env.subst(target)[0])

    # some boards (e.g. nrf51 modify the resulting firmware)
    if framework_processor.has_target_hook():
        firmware_file = env.subst(os.path.join("$BUILD_DIR", "${PROGNAME}.hex"))
        if not os.path.isfile(firmware_file):
            shutil.copyfile(env.subst(source)[0], firmware_file)

        framework_processor.apply_hook(
            env.subst(os.path.join("$BUILD_DIR/$PROGNAME$PROGSUFFIX")), firmware_file
        )


new_builders = env.get("BUILDERS", {})
new_builders["MergeHex"] = Builder(action=merge_firmwares, suffix=".hex")
env.Replace(BUILDERS=new_builders)

#
# Add legacy libs as standalone
#

if not MBED_RTOS:
    legacy_libs = (
        os.path.join(FRAMEWORK_DIR, "features", "unsupported", "dsp"),
        os.path.join(FRAMEWORK_DIR, "features", "unsupported", "rpc"),
        os.path.join(FRAMEWORK_DIR, "features", "unsupported", "USBDevice"),
        os.path.join(FRAMEWORK_DIR, "features", "unsupported", "USBHost"),
    )

    for lib in legacy_libs:
        env.Append(
            EXTRA_LIB_BUILDERS=[
                PlatformIOLibBuilder(
                    env, os.path.join(FRAMEWORK_DIR, lib), get_dynamic_manifest(lib)
                )
            ]
        )
