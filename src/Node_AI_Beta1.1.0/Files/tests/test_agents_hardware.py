from src.core.roles import ROLES, role_prompt
from src.core.settings import ModelSlot, SettingsStore, load_dotenv
from src.local_engine import hardware_check as hc


def test_hardware_never_invents_gpu(monkeypatch):
    monkeypatch.setattr(hc, "_nvidia", lambda: [])
    monkeypatch.setattr(hc, "_other_gpus", lambda skip_nvidia: [])
    hw = hc.detect(refresh=True)
    assert hw.gpus == [] and hw.total_vram_gb == 0
    assert hc.resolve_gpu_layers("auto", -1, hw) == (0, "")


def test_gpu_layers_respect_real_engine():
    gpu = hc.Gpu("RTX", "nvidia", 12.0, backend="cuda")
    cpu_only = hc.Hardware(gpus=[gpu], llama_cpp_installed=True, llama_cpp_gpu=False)
    assert hc.resolve_gpu_layers("auto", -1, cpu_only)[0] == 0
    assert "solo CPU" in hc.resolve_gpu_layers("gpu", -1, cpu_only)[1]
    full = hc.Hardware(gpus=[gpu], llama_cpp_installed=True, llama_cpp_gpu=True)
    assert hc.resolve_gpu_layers("auto", -1, full)[0] == -1
    assert hc.resolve_gpu_layers("cpu", -1, full)[0] == 0


def test_nvidia_parsing(monkeypatch):
    monkeypatch.setattr(hc.shutil, "which", lambda n: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hc, "_run", lambda *a, **k: "NVIDIA GeForce RTX 3060, 12288, 1024, 551.23\nNVIDIA T400, [N/A], [N/A], 551.23\n")
    g = hc._nvidia()
    assert g[0].vram_gb == 12.0 and g[0].vram_free_gb == 11.0 and g[1].vram_gb is None


def test_settings_roundtrip_and_unknown_keys(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    st.add_slot().model = "m"
    st.settings.director.model = "d"
    st.save()
    raw = (tmp_path / "c.json").read_text()
    (tmp_path / "c.json").write_text(raw.replace('"rounds"', '"campo_futuro": 1, "rounds"'))
    st2 = SettingsStore(tmp_path / "c.json")
    assert st2.settings.slots[0].model == "m" and st2.settings.director.model == "d"


def test_slot_limit(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    for _ in range(25):
        st.add_slot()
    assert len(st.settings.slots) == 20


def test_dotenv_crlf_and_placeholder(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_bytes(b"OPENAI_API_KEY=tu_clave_aqui\r\nGROQ_API_KEY=\"abc\"\r\n")
    load_dotenv(f)
    import os
    assert "OPENAI_API_KEY" not in os.environ and os.environ["GROQ_API_KEY"] == "abc"


def test_slot_readiness(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not ModelSlot(provider="openai", model="x").is_ready()
    assert ModelSlot(provider="openai", model="x", api_key="k").is_ready()
    assert ModelSlot(provider="ollama", model="llama3", mode="local").is_ready()
    assert not ModelSlot(provider="gguf", mode="local").is_ready()


def test_roles():
    assert {"debater", "critic", "moderator"} <= set(ROLES)
    assert "idioma" in role_prompt("critic")
