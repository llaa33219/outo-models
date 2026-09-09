class _Recorder:
    """Stand-in progress sink capturing task descriptions for assertions."""

    def __init__(self) -> None:
        self.descriptions: list[str] = []
        self.advances: list[int] = []
        self._next = 0

    def add_task(self, description: str, *, total: float | None = None) -> int:
        self.descriptions.append(description)
        self._next += 1
        return self._next - 1

    def update(self, task_id: int, advance: float = 0, **kwargs: object) -> None:
        self.advances.append(int(advance))

    def remove_task(self, task_id: int) -> None:
        return None


class TestUploadObjectsLabels:
    """Users track file NAMES, not sha prefixes (the field complaint)."""

    def test_tasks_are_named_after_files(self, tmp_path, monkeypatch):
        import httpx

        from outo_models_cli.api.lfs_batch import BatchAction
        from outo_models_cli.api.lfs_upload import upload_objects

        big = tmp_path / "weights-00001-of-00002.safetensors"
        big.write_bytes(b"x" * 4096)
        action = BatchAction(
            oid="a" * 64, size=big.stat().st_size, href="http://srv/put", header={}
        )
        recorder = _Recorder()

        def fake_put(client, **kwargs):
            pass

        monkeypatch.setattr("outo_models_cli.api.lfs_upload._put_stream", fake_put)
        client = httpx.Client()
        try:
            upload_objects(
                client,
                actions=[action],
                paths_by_oid={action.oid: [big]},
                show_progress=False,
                progress=recorder,
            )
        finally:
            client.close()
        assert "weights-00001-of-00002.safetensors" in recorder.descriptions
        assert any(d.startswith("Total (1 object") for d in recorder.descriptions)


class TestPartitionHashingProgress:
    def test_show_progress_true_does_not_crash(self, tmp_path):
        from outo_models_cli.api.lfs import partition_files

        big = tmp_path / "big.bin"
        big.write_bytes(b"y" * 2048)
        small = tmp_path / "small.txt"
        small.write_bytes(b"y")
        partition = partition_files([big, small], cap_bytes=512, show_progress=True)
        assert [f.name for f in partition.small] == ["small.txt"]
        assert partition.large and partition.large[0].path.name == "big.bin"
