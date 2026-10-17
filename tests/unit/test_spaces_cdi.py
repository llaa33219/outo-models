"""Unit tests for `outo_models.spaces.cdi`.

The module exists for one reason: podman < 6.0 has no `gpus` field on its
REST create body (Go drops the unknown key silently), so assigned GPUs must
be translated into plain SpecGenerator `devices` / `mounts` / `env` edits
that every podman version understands. These tests pin the translation
against a synthetic NVIDIA-style spec file — no host CDI registry needed.

# allow: SIZE_OK — one spec file, one translation function; splitting the
# loader and the resolver tests across files would double the fixtures
# without separating a second behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outo_models.exceptions import ValidationFailedError
from outo_models.spaces.cdi import CdiEdits, load_specs, resolve_device

_NVIDIA_SPEC = """\
cdiVersion: 0.5.0
kind: nvidia.com/gpu
containerEdits:
  deviceNodes:
    - path: /dev/nvidiactl
      type: c
      major: 255
      minor: 0
      fileMode: 0666
  mounts:
    - hostPath: /usr/lib/x86_64-linux-gnu/libcuda.so.1
      containerPath: /usr/lib/x86_64-linux-gnu/libcuda.so.1
      type: bind
      options:
        - bind
        - ro
  env:
    - NVIDIA_VISIBLE_DEVICES=all
devices:
  "0":
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia0
          type: c
          major: 195
          minor: 0
          fileMode: 0666
      env:
        - NVIDIA_VISIBLE_DEVICES=void
  "all":
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia0
          type: c
          major: 195
          minor: 0
        - path: /dev/nvidia1
          type: c
          major: 195
          minor: 1
"""


def _spec_dir(tmp_path: Path, text: str = _NVIDIA_SPEC, name: str = "nvidia.yaml") -> Path:
    directory = tmp_path / "cdi"
    directory.mkdir()
    (directory / name).write_text(text, encoding="utf-8")
    return directory


class TestLoadSpecs:
    def test_reads_yaml_spec_with_kind_and_devices(self, tmp_path: Path) -> None:
        # Given: a spec file on disk in the probed directory.
        directory = _spec_dir(tmp_path)
        # When
        specs, warnings = load_specs([directory])
        # Then
        assert warnings == []
        assert [spec["kind"] for spec in specs] == ["nvidia.com/gpu"]
        assert set(specs[0]["devices"]) == {"0", "all"}

    def test_reads_json_spec(self, tmp_path: Path) -> None:
        # JSON is a YAML subset, and some hosts ship `.json` specs.
        directory = _spec_dir(
            tmp_path,
            '{"kind": "example.com/gpu", "devices": {"0": {}}}',
            name="example.json",
        )
        specs, warnings = load_specs([directory])
        assert warnings == []
        assert specs[0]["kind"] == "example.com/gpu"

    def test_skips_unparseable_file_and_keeps_the_good_one(self, tmp_path: Path) -> None:
        directory = _spec_dir(tmp_path)
        (directory / "broken.yaml").write_text("kind: [unterminated\n", encoding="utf-8")
        specs, warnings = load_specs([directory])
        assert [spec["kind"] for spec in specs] == ["nvidia.com/gpu"]
        assert len(warnings) == 1
        assert "broken.yaml" in warnings[0]

    def test_absent_directory_is_not_an_error(self, tmp_path: Path) -> None:
        specs, warnings = load_specs([tmp_path / "absent"])
        assert specs == []
        assert warnings == []


class TestResolveDevice:
    def test_bare_name_resolves_under_the_default_kind(self, tmp_path: Path) -> None:
        edits = resolve_device(["0"], dirs=[_spec_dir(tmp_path)])
        assert isinstance(edits, CdiEdits)
        # Global edits apply to every device of the spec…
        assert edits.env == {"NVIDIA_VISIBLE_DEVICES": "void"}
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidiactl", "/dev/nvidia0"]
        # …and the per-device env overrides the global one.
        assert edits.mounts == (
            {
                "source": "/usr/lib/x86_64-linux-gnu/libcuda.so.1",
                "destination": "/usr/lib/x86_64-linux-gnu/libcuda.so.1",
                "type": "bind",
                "options": ["bind", "ro"],
            },
        )

    @pytest.mark.parametrize("identifier", ["0", "gpu-0", "nvidia.com/gpu=0"])
    def test_identifier_forms_are_equivalent(self, tmp_path: Path, identifier: str) -> None:
        directory = _spec_dir(tmp_path)
        assert resolve_device([identifier], dirs=[directory]) == resolve_device(
            ["0"], dirs=[directory]
        )

    def test_all_device_injects_every_node(self, tmp_path: Path) -> None:
        edits = resolve_device(["all"], dirs=[_spec_dir(tmp_path)])
        assert [node["path"] for node in edits.device_nodes] == [
            "/dev/nvidiactl",
            "/dev/nvidia0",
            "/dev/nvidia1",
        ]

    def test_fully_qualified_kind_wins_over_the_default(self, tmp_path: Path) -> None:
        # Two specs in one directory: a bare name must NOT fall back to the
        # other kind's device just because it is the only one that has it.
        directory = _spec_dir(tmp_path)
        (directory / "other.yaml").write_text(
            "kind: example.com/gpu\n"
            "containerEdits:\n"
            "  deviceNodes:\n"
            "    - path: /dev/example0\n"
            "      type: c\n"
            "      major: 200\n"
            "      minor: 0\n"
            'devices:\n  "0": {}\n',
            encoding="utf-8",
        )
        edits = resolve_device(["example.com/gpu=0"], dirs=[directory])
        assert [node["path"] for node in edits.device_nodes] == ["/dev/example0"]

    def test_multiple_identifiers_merge_every_device(self, tmp_path: Path) -> None:
        edits = resolve_device(["0", "all"], dirs=[_spec_dir(tmp_path)])
        # Both identifiers contribute their own edits plus the spec-wide ones.
        assert [node["path"] for node in edits.device_nodes].count("/dev/nvidiactl") == 2

    def test_device_node_keeps_only_present_keys(self, tmp_path: Path) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n",
        )
        edits = resolve_device(["0"], dirs=[directory])
        assert edits.device_nodes == ({"path": "/dev/nvidia0"},)

    def test_hostpath_device_node_yields_bind_mount_plus_node(self, tmp_path: Path) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/dri/card0\n"
            "          hostPath: /dev/dri/card0\n"
            "          type: c\n"
            "          major: 226\n"
            "          minor: 0\n"
            "          fileMode: 0666\n",
        )
        edits = resolve_device(["0"], dirs=[directory])
        assert edits.mounts == (
            {
                "source": "/dev/dri/card0",
                "destination": "/dev/dri/card0",
                "type": "bind",
                "options": ["bind"],
            },
        )
        assert edits.device_nodes == (
            {
                "path": "/dev/dri/card0",
                "type": "c",
                "major": 226,
                "minor": 0,
                "fileMode": 0o666,
            },
        )


class TestResolveDeviceFailures:
    def test_missing_device_names_identifier_kind_and_dirs(self, tmp_path: Path) -> None:
        directory = _spec_dir(tmp_path)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["3"], dirs=[directory])
        message = str(excinfo.value)
        assert "3" in message
        assert "nvidia.com/gpu" in message
        assert str(directory) in message
        assert excinfo.value.code == "space_gpu_cdi_unresolved"

    def test_empty_spec_directory_names_the_missing_kind(self, tmp_path: Path) -> None:
        # The dirs are mounted but hold no spec of this kind — the classic
        # "GPU assigned, container silently GPU-less" setup.
        empty = tmp_path / "cdi"
        empty.mkdir()
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[empty])
        assert "nvidia.com/gpu" in str(excinfo.value)
        assert str(empty) in str(excinfo.value)

    def test_hooks_are_refused_loudly(self, tmp_path: Path) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      hooks:\n"
            "        - hookName: createContainer\n"
            "          path: /bin/true\n",
        )
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[directory])
        assert "hooks" in str(excinfo.value)
        assert "0" in str(excinfo.value)

    def test_unsupported_edit_kind_is_refused_loudly(self, tmp_path: Path) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      additionalGIDs: [44]\n",
        )
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[directory])
        assert "additionalGIDs" in str(excinfo.value)
