"""firmware-bench auto-prepends select_i2c_strand when a strand is required."""

from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter


def _adapter(*, auto_strand_id, stages):
    return FirmwareBenchAdapter(
        controller_transport=object(),
        dut_transport=object(),
        hub_transport=object(),
        job_id="job1",
        device={"id": "qtpy"},
        params={"stages": stages},
        auto_strand_id=auto_strand_id,
    )


def test_prepends_select_stage_when_strand_required():
    a = _adapter(auto_strand_id="strand-air", stages=[{"type": "flash"}])
    stages = a._build_stages(log_port="")
    assert stages[0] == {"type": "select_i2c_strand", "strand_id": "strand-air"}


def test_no_prepend_without_required_strand():
    a = _adapter(auto_strand_id=None, stages=[{"type": "flash"}])
    stages = a._build_stages(log_port="")
    assert not any(s.get("type") == "select_i2c_strand" for s in stages)


def test_does_not_duplicate_explicit_select_stage():
    explicit = [
        {"type": "select_i2c_strand", "strand_id": "strand-air", "channel": 1},
        {"type": "flash"},
    ]
    a = _adapter(auto_strand_id="strand-air", stages=explicit)
    stages = a._build_stages(log_port="")
    selects = [s for s in stages if s.get("type") == "select_i2c_strand"]
    assert len(selects) == 1
    assert selects[0].get("channel") == 1  # operator's explicit stage preserved
