import hashlib
import json
from pathlib import Path
import resource
import signal
import subprocess
import sys

CANDIDATE = "5b60e2bc72f0c9136851a1f17205b3937c8a3ed4"
BASE = "1ca121740cf4fcda586e1ce665962f8b936e790b"
SOURCE = Path("src/abi/pass_mode.rs")
RESULTS = Path("control-results")
RESULTS.mkdir(exist_ok=True)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def git_file(rev, path):
    return subprocess.check_output(["git", "show", f"{rev}:{path}"], text=True)


def run(args, label):
    print(label, args, flush=True)
    p = subprocess.run(args, capture_output=True, text=True)
    (RESULTS / f"{label}.stdout").write_text(p.stdout)
    (RESULTS / f"{label}.stderr").write_text(p.stderr)
    identity = {"args": args, "exit_code": p.returncode,
                "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()}
    for backend in Path("dist/lib").glob("*rustc_codegen_cranelift*"):
        identity["backend_sha256"] = hashlib.sha256(backend.read_bytes()).hexdigest()
    (RESULTS / f"{label}.json").write_text(json.dumps(identity, indent=2) + "\n")
    print(label, "exit", p.returncode, flush=True)
    if p.returncode:
        print(p.stderr[-5000:], flush=True)
    return p


def abi(candidate):
    harness = Path("build_system/abi_cafe.rs")
    harness.write_text(harness.read_text().replace(
        '    cmd.arg("--debug");',
        '    cmd.arg("--debug");\n'
        '    cmd.arg("--tests").arg("SingleVariantUnion");\n'
        '    cmd.arg("--output-format").arg("json");'))
    old = git_file(BASE, str(SOURCE))
    start = "fn cast_target_to_abi_params("
    old_fn = old[old.index(start):old.index("\nimpl<'tcx>", old.index(start))]
    a = candidate.index(start)
    b = candidate.index("\nfn abi_params_size", a)
    control = candidate[:a] + old_fn + candidate[b:]
    for label, source, fails in [("candidate", candidate, False),
                                  ("old-abi-units", control, True),
                                  ("restored", candidate, False)]:
        SOURCE.write_text(source)
        p = run(["./y.sh", "abi-cafe", "--sysroot", "llvm"], label)
        report = json.loads(p.stdout)
        assert report["summary"]["num_tests"] > 0
        failures = [t for t in report["tests"] if t["conclusion"] == "failed"]
        print(label, report["summary"], flush=True)
        assert all(t["key"]["test"] == "SingleVariantUnion" for t in report["tests"])
        if fails:
            assert p.returncode != 0 and failures
            for t in failures:
                assert "Ok" in t["results"]["build"]
                assert "Ok" in t["results"]["link"]
                assert "Ok" in t["results"]["run"]
                assert t["results"]["check"]["all_passed"] is False
                print("Observed value mismatch:", t["key"], flush=True)
        else:
            assert p.returncode == 0 and not failures
            assert report["summary"]["num_passed"] > 0


def endian(candidate):
    wrong = candidate.replace("Endian::Big => bytes - loaded - load_bytes,",
                              "Endian::Big => loaded,")
    assert wrong != candidate
    for label, source, fails in [("candidate", candidate, False),
                                  ("wrong-endian", wrong, True),
                                  ("restored", candidate, False)]:
        SOURCE.write_text(source)
        p = run(["./y.sh", "build", "--sysroot", "llvm", "--no-unstable-features"], label + "-build")
        assert p.returncode == 0
        exe = str(Path.cwd() / "abi-cast-control")
        p = run(["./dist/rustc-clif", "--edition=2024", "-Dwarnings", "-Zvalidate-mir",
                 "--target=s390x-unknown-linux-gnu", "-Clinker=s390x-linux-gnu-gcc",
                 "example/abi_cast.rs", "-o", exe], label + "-compile")
        assert p.returncode == 0
        p = run(["qemu-s390x", "-L", "/usr/s390x-linux-gnu", exe], label + "-run")
        if fails:
            assert p.returncode in (-signal.SIGABRT, 128 + signal.SIGABRT)
            assert "assertion `left == right` failed" in p.stderr
        else:
            assert p.returncode == 0


candidate = git_file(CANDIDATE, str(SOURCE))
assert SOURCE.read_text() == candidate
assert Path("example/abi_cast.rs").read_text() == git_file(CANDIDATE, "example/abi_cast.rs")
try:
    {"abi": abi, "endian": endian}[sys.argv[1]](candidate)
finally:
    SOURCE.write_text(candidate)
