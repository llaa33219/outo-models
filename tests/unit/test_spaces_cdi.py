"""Unit tests for `outo_models.spaces.cdi`.

The module exists for one reason: when podman refuses a create that carried
CDI device names (podman < 6.0 has no `gpus` field — Go drops that unknown key
silently), the runtime must say what the host's specs actually contain. These
tests pin that resolution against a synthetic NVIDIA-style spec file — no
host CDI registry needed.

# allow: SIZE_OK — one spec file, one translation function; splitting the
# loader and the resolver tests across files would double the fixtures
# without separating a second behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outo_models.exceptions import ValidationFailedError
from outo_models.spaces.cdi import (
    CdiEdits,
    load_specs,
    parse_specs,
    resolve_device,
    resolve_edits,
    resolve_names,
)

_OTHER_SPEC = """\
cdiVersion: 0.5.0
kind: example.com/gpu
devices:
  "0":
    containerEdits:
      deviceNodes:
        - path: /dev/example0
"""

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


#: The shape `nvidia-ctk cdi generate` actually writes: `devices` is a LIST of
#: `{name, containerEdits}` entries (cncf-tags/container-device-interface
#: specs-go/config.go). Same content as `_NVIDIA_SPEC`, canonical schema.
_NVIDIA_LIST_SPEC = """\
cdiVersion: 0.5.0
kind: nvidia.com/gpu
containerEdits:
  deviceNodes:
    - path: /dev/nvidiactl
      type: c
      major: 255
      minor: 0
      fileMode: 0666
  env:
    - NVIDIA_VISIBLE_DEVICES=all
devices:
  - name: "0"
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia0
          type: c
          major: 195
          minor: 0
          fileMode: 0666
      env:
        - NVIDIA_VISIBLE_DEVICES=void
  - name: "1"
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia1
          type: c
          major: 195
          minor: 1
  - name: "all"
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


#: A `gpu-0`-shaped spec: podman accepts `nvidia.com/gpu=gpu-0` and nothing
#: else, whatever spelling the assignment used.
_ALIASED_DEVICE_SPEC = """\
kind: nvidia.com/gpu
devices:
  - name: "gpu-0"
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia0
"""

#: Two devices whose names differ only by the alias rule's `gpu-` prefix.
_TWO_ALIASED_DEVICES_SPEC = """\
kind: nvidia.com/gpu
devices:
  - name: "gpu-0"
  - name: "0"
"""

#: Two `gpu-`-indexed devices, as a multi-GPU assignment spells them.
_TWO_GPU_INDEXED_DEVICES_SPEC = """\
kind: nvidia.com/gpu
devices:
  - name: "gpu-0"
  - name: "gpu-1"
"""

_UUID_DEVICE_SPEC = """\
kind: nvidia.com/gpu
devices:
  - name: "GPU-1a2b3c4d-0000-0000-0000-000000000000"
    containerEdits:
      deviceNodes:
        - path: /dev/nvidia0
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

    def test_missing_device_lists_the_names_the_spec_defines(self, tmp_path: Path) -> None:
        # Given: a canonical spec defining three devices.
        directory = _spec_dir(tmp_path, _NVIDIA_LIST_SPEC)
        # When
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["7"], dirs=[directory])
        # Then: the operator can see at a glance whether the assignment is
        # wrong or the spec is missing the device.
        assert "defined devices: 0, 1, all" in str(excinfo.value)
        assert excinfo.value.code == "space_gpu_cdi_unresolved"

    def test_missing_device_list_is_capped(self) -> None:
        names = [str(index) for index in range(20)]
        specs, _ = parse_specs(
            "kind: nvidia.com/gpu\ndevices:\n" + "".join(f'  - name: "{name}"\n' for name in names)
        )
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_edits(specs, ["99"], searched=("/etc/cdi",))
        assert f"defined devices: {', '.join(sorted(names)[:12])}…" in str(excinfo.value)

    def test_empty_spec_directory_names_the_missing_kind(self, tmp_path: Path) -> None:
        # The dirs are mounted but hold no spec of this kind — the classic
        # "GPU assigned, container silently GPU-less" setup.
        empty = tmp_path / "cdi"
        empty.mkdir()
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[empty])
        assert "nvidia.com/gpu" in str(excinfo.value)
        assert str(empty) in str(excinfo.value)

    def test_missing_spec_message_gives_the_full_recovery(self, tmp_path: Path) -> None:
        # The field failure: a host WITH valid /etc/cdi specs still hit this,
        # because the server container could not see them. The message has to
        # carry every step of the recovery, not just "install the specs".
        empty = tmp_path / "cdi"
        empty.mkdir()
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[empty])
        message = str(excinfo.value)
        assert "nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml" in message
        assert "install-cli.sh" in message
        assert "outo-models update" in message
        assert excinfo.value.code == "space_gpu_cdi_unresolved"

    def test_hooks_are_ignored_without_failing_the_resolution(self, tmp_path: Path) -> None:
        # Given: a spec whose device carries OCI hooks — the shape the live
        # NVIDIA spec has. podman applies hooks itself on its own CDI path
        # (registry.InjectDevices), so their presence must not make the
        # assignment look unresolvable.
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      hooks:\n"
            "        - hookName: createContainer\n"
            "          path: /bin/true\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n"
            "          type: c\n"
            "          major: 195\n"
            "          minor: 0\n",
        )
        # When
        edits = resolve_device(["0"], dirs=[directory])
        # Then: the device resolved and its expressible edits came through;
        # the hooks were skipped, not refused.
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidia0"]

    def test_dropped_hooks_are_reported_as_a_note(self, tmp_path: Path) -> None:
        # Given: the same hook-carrying spec. The hooks cannot be expressed
        # in a create body, but dropping them silently would ship a container
        # that is not the one the spec describes.
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      hooks:\n"
            "        - hookName: createContainer\n"
            "          path: /bin/true\n"
            "        - hookName: startContainer\n"
            "          path: /bin/true\n",
        )
        # When
        edits = resolve_device(["0"], dirs=[directory])
        # Then: the note names the hooks that did not make it.
        assert len(edits.notes) == 1
        assert "hooks dropped" in edits.notes[0]
        assert "createContainer, startContainer" in edits.notes[0]

    def test_a_spec_without_hooks_reports_no_notes(self, tmp_path: Path) -> None:
        # When
        edits = resolve_device(["0"], dirs=[_spec_dir(tmp_path)])
        # Then: nothing was left behind, so the caller has nothing to say.
        assert edits.notes == ()

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


class TestParseSpecs:
    """`parse_specs` is the IO-free half of the loader.

    The host-read fallback in `runtime_manager` gets its specs as one
    `---`-separated text stream instead of from local files, so parsing has
    to be reachable without a filesystem.
    """

    def test_parses_every_document_of_a_separated_stream(self) -> None:
        # Given: the concatenation a helper container produces — one `---`
        # marker in front of each spec file it catted.
        stream = f"---\n{_NVIDIA_SPEC}\n---\n{_OTHER_SPEC}"
        # When
        specs, warnings = parse_specs(stream)
        # Then: document order is preserved — a device must never resolve
        # against a spec that was concatenated after it.
        assert warnings == []
        assert [spec["kind"] for spec in specs] == ["nvidia.com/gpu", "example.com/gpu"]

    def test_skips_an_empty_document_without_warning(self) -> None:
        # An empty spec file on the host yields an empty document, which is
        # "nothing to read", not "something unreadable".
        specs, warnings = parse_specs(f"---\n---\n{_OTHER_SPEC}")
        assert warnings == []
        assert [spec["kind"] for spec in specs] == ["example.com/gpu"]

    def test_warns_about_a_non_mapping_document(self) -> None:
        specs, warnings = parse_specs(f"---\n{_NVIDIA_SPEC}\n---\n- just\n- a\n- list\n")
        assert [spec["kind"] for spec in specs] == ["nvidia.com/gpu"]
        assert len(warnings) == 1
        assert "not a mapping" in warnings[0]

    def test_unparseable_stream_keeps_every_valid_document(self) -> None:
        specs, warnings = parse_specs("---\nkind: [unterminated\n")
        assert specs == []
        assert len(warnings) == 1


class TestResolveEdits:
    """`resolve_edits` is the same pure translation as `resolve_device`,
    with the specs handed in instead of read from disk."""

    def test_matches_the_filesystem_backed_translation(self, tmp_path: Path) -> None:
        specs, _ = load_specs([_spec_dir(tmp_path)])
        assert resolve_edits(specs, ["0"], searched=(str(tmp_path / "cdi"),)) == resolve_device(
            ["0"], dirs=[tmp_path / "cdi"]
        )

    def test_identifier_forms_are_normalized(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        assert resolve_edits(specs, ["gpu-0"]) == resolve_edits(specs, ["nvidia.com/gpu=0"])

    def test_qualified_kind_wins_over_the_default_kind(self) -> None:
        specs, _ = parse_specs(f"---\n{_NVIDIA_SPEC}\n---\n{_OTHER_SPEC}")
        edits = resolve_edits(specs, ["example.com/gpu=0"])
        assert [node["path"] for node in edits.device_nodes] == ["/dev/example0"]

    def test_missing_kind_reports_the_searched_dirs(self, tmp_path: Path) -> None:
        specs, _ = parse_specs(_OTHER_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_edits(specs, ["0"], searched=("/etc/cdi",))
        message = str(excinfo.value)
        assert "nvidia.com/gpu" in message
        assert "/etc/cdi" in message
        assert excinfo.value.code == "space_gpu_cdi_unresolved"

    def test_context_is_appended_to_the_missing_device_error(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_edits(
                specs,
                ["3"],
                searched=("/etc/cdi",),
                context="specs read from the host via a helper container",
            )
        message = str(excinfo.value)
        assert "3" in message
        assert "specs read from the host via a helper container" in message

    def test_context_is_appended_to_an_unsupported_edit_error(self) -> None:
        specs, _ = parse_specs(
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      additionalGIDs: [44]\n"
        )
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_edits(specs, ["0"], context="specs read from the host via a helper container")
        assert "additionalGIDs" in str(excinfo.value)
        assert "helper container" in str(excinfo.value)

    def test_context_is_omitted_by_default(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_edits(specs, ["3"], searched=("/etc/cdi",))
        assert "helper container" not in str(excinfo.value)


class TestCanonicalListFormSpec:
    """The canonical CDI schema declares `devices` as a LIST of entries.

    The map-shaped fixtures above covered only our own lenient reading: a
    real `nvidia-ctk` spec parses fine — readable documents, matching kind —
    yet no device was ever found, so the GPU never reached the container.
    """

    def test_list_form_device_merges_global_and_device_edits(self, tmp_path: Path) -> None:
        directory = _spec_dir(tmp_path, _NVIDIA_LIST_SPEC)
        # When
        edits = resolve_device(["0"], dirs=[directory])
        # Then: the spec-wide edits apply, then the device's own ones.
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidiactl", "/dev/nvidia0"]
        assert edits.env == {"NVIDIA_VISIBLE_DEVICES": "void"}

    @pytest.mark.parametrize("identifier", ["0", "gpu-0", "nvidia.com/gpu=0"])
    def test_identifier_forms_resolve_to_the_same_device(
        self, tmp_path: Path, identifier: str
    ) -> None:
        directory = _spec_dir(tmp_path, _NVIDIA_LIST_SPEC)
        assert resolve_device([identifier], dirs=[directory]) == resolve_device(
            ["0"], dirs=[directory]
        )

    def test_each_list_entry_resolves_its_own_device(self, tmp_path: Path) -> None:
        directory = _spec_dir(tmp_path, _NVIDIA_LIST_SPEC)
        edits = resolve_device(["1"], dirs=[directory])
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidiactl", "/dev/nvidia1"]


class TestDeviceNameAliasing:
    """A spec and an assignment may spell the same GPU differently."""

    def test_gpu_prefixed_device_answers_a_bare_index_query(self, tmp_path: Path) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  - name: "gpu-0"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n",
        )
        assert resolve_device(["0"], dirs=[directory]).device_nodes == ({"path": "/dev/nvidia0"},)

    def test_bare_index_device_answers_a_gpu_prefixed_query(self, tmp_path: Path) -> None:
        directory = _spec_dir(tmp_path, _NVIDIA_LIST_SPEC)
        assert resolve_device(["gpu-0"], dirs=[directory]) == resolve_device(
            ["0"], dirs=[directory]
        )

    def test_exact_name_wins_over_a_normalized_alias(self, tmp_path: Path) -> None:
        # Both names are defined, so "0" must resolve to the entry literally
        # called "0" rather than to the "gpu-0" entry that aliases it.
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            '  - name: "gpu-0"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/aliased\n"
            '  - name: "0"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/exact\n",
        )
        assert [node["path"] for node in resolve_device(["0"], dirs=[directory]).device_nodes] == [
            "/dev/exact"
        ]


class TestMalformedDeviceEntries:
    """One broken device entry must not take the whole spec down with it."""

    def test_malformed_entries_are_skipped_and_warned_about(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # Given: a canonical list whose first two entries are unusable.
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            "  - just-a-string\n"
            "  - containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nameless\n"
            '  - name: "0"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n",
        )
        # When
        edits = resolve_device(["0"], dirs=[directory])
        # Then: the good device still resolves, and the bad entries said so.
        # fd-level capture: structlog's capture_logs is config-order dependent.
        captured = capfd.readouterr()
        text = captured.out + captured.err
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidia0"]
        assert "entry 1 is not a mapping" in text
        assert "entry 2 has no name" in text

    def test_malformed_map_entry_is_skipped_and_warned_about(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            "  broken: not-a-mapping\n"
            '  "0":\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n",
        )
        edits = resolve_device(["0"], dirs=[directory])
        captured = capfd.readouterr()
        assert [node["path"] for node in edits.device_nodes] == ["/dev/nvidia0"]
        assert "device 'broken' is not a mapping" in captured.out + captured.err

    def test_a_malformed_entry_is_reported_once_per_spec(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        directory = _spec_dir(
            tmp_path,
            "kind: nvidia.com/gpu\n"
            "devices:\n"
            "  - just-a-string\n"
            '  - name: "0"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia0\n"
            '  - name: "all"\n'
            "    containerEdits:\n"
            "      deviceNodes:\n"
            "        - path: /dev/nvidia1\n",
        )
        resolve_device(["0", "all"], dirs=[directory])
        captured = capfd.readouterr()
        assert (captured.out + captured.err).count("entry 1 is not a mapping") == 1


class TestMissingSpecDiagnostics:
    """The missing-spec error reports what the resolver actually observed.

    "Specs missing on the host", "specs mounted but unparseable", "specs of
    a foreign kind" and "the directory was never mounted" all used to raise
    the same message, so an operator had no way to tell them apart from
    inside the server container. Each line of the report is one dir.
    """

    def test_existing_directory_reports_the_kind_it_found(self, tmp_path: Path) -> None:
        # Given: the dir is mounted and readable, but holds another kind.
        directory = _spec_dir(
            tmp_path,
            '{"kind": "example.com/gpu", "devices": {"0": {}}}',
            name="example.json",
        )
        # When
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[directory])
        # Then
        message = str(excinfo.value)
        assert f"{directory}: present, 1 readable spec" in message
        assert "kinds: example.com/gpu" in message

    def test_absent_directory_is_reported_as_missing(self, tmp_path: Path) -> None:
        # Given: one mounted dir and one that was never mounted at all.
        absent = tmp_path / "not-mounted"
        directory = _spec_dir(
            tmp_path,
            '{"kind": "example.com/gpu", "devices": {"0": {}}}',
            name="example.json",
        )
        # When
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[directory, absent])
        # Then
        message = str(excinfo.value)
        assert f"{absent}: missing" in message
        assert f"{directory}: present," in message

    def test_empty_directory_is_reported_as_zero_specs(self, tmp_path: Path) -> None:
        # Given: a mounted but empty spec dir.
        empty = tmp_path / "cdi"
        empty.mkdir()
        # When
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[empty])
        # Then
        assert f"{empty}: present, 0 readable specs, kinds: none" in str(excinfo.value)

    def test_unparseable_specs_are_counted_separately_from_an_empty_dir(
        self, tmp_path: Path
    ) -> None:
        # Given: the dir is mounted and holds a spec file the resolver could
        # not parse — the case that reads exactly like an empty dir otherwise.
        directory = _spec_dir(tmp_path, "kind: [unterminated\n", name="broken.yaml")
        # When
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_device(["0"], dirs=[directory])
        # Then
        expected = f"{directory}: present, 0 readable specs (1 unreadable), kinds: none"
        assert expected in str(excinfo.value)


class TestResolveNames:
    """`resolve_names` answers the delivery question: what does podman want?

    Podman's CDI registry matches device names EXACTLY — `nvidia.com/gpu=0` is
    unresolvable on a host whose spec names its device `gpu-0`. The create
    body therefore has to carry the name the spec itself defines, which is a
    different question from "does this assignment mean that device?".
    """

    def test_exact_device_name_is_returned_verbatim(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        assert resolve_names(specs, ["0"]) == ("0",)

    @pytest.mark.parametrize("identifier", ["0", "gpu-0", "nvidia.com/gpu=gpu-0"])
    def test_alias_match_returns_the_spec_spelling(self, identifier: str) -> None:
        # Given: a `gpu-0`-shaped spec — the live GB10 case. A bare index, the
        # admin-UI spelling and a qualified form all mean the same device.
        specs, _ = parse_specs(_ALIASED_DEVICE_SPEC)
        # When
        names = resolve_names(specs, [identifier])
        # Then: the spec's own name, never the query's spelling.
        assert names == ("gpu-0",)

    def test_bare_index_spec_answers_a_gpu_prefixed_query(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        assert resolve_names(specs, ["gpu-0"]) == ("0",)

    def test_uuid_device_passes_through_unchanged(self) -> None:
        specs, _ = parse_specs(_UUID_DEVICE_SPEC)
        uuid = "GPU-1a2b3c4d-0000-0000-0000-000000000000"
        assert resolve_names(specs, [uuid]) == (uuid,)

    def test_all_device_keeps_its_name(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        assert resolve_names(specs, ["all"]) == ("all",)

    def test_every_assigned_device_keeps_its_own_spec_name(self) -> None:
        specs, _ = parse_specs(_TWO_GPU_INDEXED_DEVICES_SPEC)
        assert resolve_names(specs, ["gpu-0", "gpu-1"]) == ("gpu-0", "gpu-1")

    def test_exact_name_wins_over_a_normalized_alias(self) -> None:
        # Both names are defined: "0" must resolve to the entry literally
        # called "0", because that is the one podman will accept.
        specs, _ = parse_specs(_TWO_ALIASED_DEVICES_SPEC)
        assert resolve_names(specs, ["0"]) == ("0",)

    def test_qualified_identifier_keeps_its_own_kind(self) -> None:
        specs, _ = parse_specs(f"---\n{_NVIDIA_SPEC}\n---\n{_OTHER_SPEC}")
        assert resolve_names(specs, ["example.com/gpu=0"]) == ("0",)

    def test_missing_device_names_the_defined_ones(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_names(specs, ["GPU-nonexistent"], searched=("/etc/cdi",))
        message = str(excinfo.value)
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "defined devices: 0, all" in message

    def test_missing_kind_reports_the_searched_dirs(self) -> None:
        specs, _ = parse_specs(_OTHER_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_names(specs, ["0"], searched=("/etc/cdi",))
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "What the resolver actually saw" in str(excinfo.value)

    def test_context_is_appended_to_the_missing_device_error(self) -> None:
        specs, _ = parse_specs(_NVIDIA_SPEC)
        with pytest.raises(ValidationFailedError) as excinfo:
            resolve_names(specs, ["3"], context="specs read from the host via a helper container")
        assert "specs read from the host via a helper container" in str(excinfo.value)
