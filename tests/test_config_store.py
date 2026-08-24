import json
import pytest
from utils import config_store


DEFAULTS = {"engineeringMode": False, "clinicalMode": False, "leftMask": 0x66, "bfiMax": 10.0}


@pytest.mark.unit
def test_load_merges_overrides_over_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"engineeringMode": True}), encoding="utf-8"
    )
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert baseline["engineeringMode"] is False      # baseline untouched
    assert merged["engineeringMode"] is True          # override wins
    assert merged["bfiMax"] == 10.0                 # untouched key flows through


@pytest.mark.unit
def test_save_writes_only_diff_against_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline = dict(DEFAULTS)
    current = {**DEFAULTS, "engineeringMode": True}
    config_store.save_overrides(current, baseline)
    written = json.loads((tmp_path / "app_config.local.json").read_text(encoding="utf-8"))
    assert written == {"engineeringMode": True}       # only the changed key


@pytest.mark.unit
def test_load_with_no_override_file_returns_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert merged == baseline


# ── Issue #233: clinicalMode is build-time only ──────────────────────────
# The Clinical/Research split is decided per artifact by the build system
# (scripts/build_common.ps1) or the OPENMOTION_CLINICAL env override —
# never by the user at runtime. A stray clinicalMode in the writable
# overrides file (e.g. left behind by a pre-1.5 build) must not flip a
# Research install into Clinical, and runtime saves must never persist it.

@pytest.mark.unit
def test_stray_clinical_override_is_ignored_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    # Point the shipped-config layer at an empty file so the repo's real
    # config/app_config.json (clinicalMode: true) can't mask the override.
    (tmp_path / "app_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENWATER_CONFIG_DIR", str(tmp_path))
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"clinicalMode": True, "bfiMax": 5.0}), encoding="utf-8"
    )
    baseline, merged = config_store.load_app_config(DEFAULTS)
    assert merged["clinicalMode"] is False   # override discarded
    assert merged["bfiMax"] == 5.0           # other overrides still apply


@pytest.mark.unit
def test_save_never_persists_clinical_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    baseline = dict(DEFAULTS)
    # e.g. OPENMOTION_CLINICAL=1 flipped only the merged copy upstream, or
    # a legacy override leaked into the running config: either way the
    # diff-vs-baseline save must not write it back to disk.
    current = {**DEFAULTS, "clinicalMode": True, "engineeringMode": True}
    config_store.save_overrides(current, baseline)
    written = json.loads(
        (tmp_path / "app_config.local.json").read_text(encoding="utf-8"))
    assert written == {"engineeringMode": True}


@pytest.mark.unit
def test_portable_mode_is_build_time_only_too(tmp_path, monkeypatch):
    """portableMode's docstring already declares it a build-time flag —
    enforce the same no-runtime-override rule as clinicalMode."""
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    defaults = {**DEFAULTS, "portableMode": False}
    (tmp_path / "app_config.local.json").write_text(
        json.dumps({"portableMode": True}), encoding="utf-8"
    )
    baseline, merged = config_store.load_app_config(defaults)
    assert merged["portableMode"] is False
    config_store.save_overrides(
        {**defaults, "portableMode": True}, dict(defaults))
    written = json.loads(
        (tmp_path / "app_config.local.json").read_text(encoding="utf-8"))
    assert written == {}


@pytest.mark.unit
def test_save_survives_unserializable_value(tmp_path, monkeypatch):
    """#446 crash regression: a non-JSON value leaking into the config (a
    QJSValue through the QML bridge) must lose the write, not the app —
    save_overrides logs and returns, keeps the prior overrides intact, and
    leaves no truncated .tmp behind."""
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))
    existing = tmp_path / "app_config.local.json"
    existing.write_text(json.dumps({"engineeringMode": True}), encoding="utf-8")

    config_store.save_overrides(
        {**DEFAULTS, "altCameraGains": object()}, dict(DEFAULTS))  # no raise

    assert json.loads(existing.read_text(encoding="utf-8")) == {
        "engineeringMode": True}
    assert not (tmp_path / "app_config.local.json.tmp").exists()
