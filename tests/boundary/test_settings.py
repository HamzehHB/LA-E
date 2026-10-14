from pathlib import Path

import pytest

from Config import settings
from Config.settings import ROOT, ConfigError, load_models, load_paths


EXAMPLE_YAML = """
data:
  root: "Q:/SyntheticMachineOnly/PrivateData"
models:
  bge_m3: "Q:/SyntheticMachineOnly/PrivateData/Models/BGE_M3"
vector_db:
  lancedb: "Q:/SyntheticMachineOnly/PrivateData/Database/Vector_Database"
memory:
  root: "Q:/SyntheticMachineOnly/PrivateData/Memory"
obsidian:
  vault: "Q:/SyntheticMachineOnly/PrivateData/Knowledge/Obsidian"
zotero:
  library: "Q:/SyntheticMachineOnly/PrivateData/Knowledge/Zotero"
exports:
  root: "Q:/SyntheticMachineOnly/PrivateData/Knowledge/Exports"
cache:
  root: "Q:/SyntheticMachineOnly/PrivateData/Database/Cache"
"""

LOCAL_OVERRIDE_YAML = """
data:
  root: "F:/MyData"
vector_db:
  lancedb: "F:/MyData/Database/Vector_Database"
"""

LOCAL_TRYING_TO_OVERRIDE_DERIVED_YAML = """
project:
  root: "Z:/Ignored"
logs:
  root: "Z:/Ignored"
prompts:
  root: "Z:/Ignored"
"""

MODELS_YAML = """
embedding:
  active: "bge_m3"
bge_m3:
  dimension: 1024
  normalize: true
  batch_size: 16
"""


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_load_paths_without_local_file_returns_example_values(tmp_path):
    _write(tmp_path, "paths.example.yaml", EXAMPLE_YAML)

    paths = load_paths(tmp_path)

    assert paths["data"]["root"] == "Q:/SyntheticMachineOnly/PrivateData"
    assert paths["vector_db"]["lancedb"] == (
        "Q:/SyntheticMachineOnly/PrivateData/Database/Vector_Database"
    )


def test_load_paths_local_file_overrides_example_values(tmp_path):
    _write(tmp_path, "paths.example.yaml", EXAMPLE_YAML)
    _write(tmp_path, "paths.local.yaml", LOCAL_OVERRIDE_YAML)

    paths = load_paths(tmp_path)

    assert paths["data"]["root"] == "F:/MyData"
    assert paths["vector_db"]["lancedb"] == "F:/MyData/Database/Vector_Database"
    assert paths["obsidian"]["vault"] == "Q:/SyntheticMachineOnly/PrivateData/Knowledge/Obsidian"


def test_load_paths_always_derives_repository_internal_locations(tmp_path):
    _write(tmp_path, "paths.example.yaml", EXAMPLE_YAML)
    _write(tmp_path, "paths.local.yaml", LOCAL_TRYING_TO_OVERRIDE_DERIVED_YAML)

    paths = load_paths(tmp_path)

    assert paths["project"]["root"] == str(ROOT)
    assert paths["logs"]["root"] == str(ROOT / "Logs")
    assert paths["prompts"]["root"] == str(ROOT / "Prompts")


def test_load_paths_missing_example_file_raises_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_paths(tmp_path)


def test_load_paths_empty_required_value_raises_clear_error(tmp_path):
    content = EXAMPLE_YAML.replace(
        '  root: "Q:/SyntheticMachineOnly/PrivateData"',
        '  root: ""',
        1,
    )

    _write(tmp_path, "paths.example.yaml", content)

    with pytest.raises(ConfigError, match="'data.root'"):
        load_paths(tmp_path)


def test_load_paths_missing_required_key_raises_clear_error(tmp_path):
    content = EXAMPLE_YAML.replace("vector_db:", "vector_db_removed:")

    _write(tmp_path, "paths.example.yaml", content)

    with pytest.raises(ConfigError, match="'vector_db.lancedb'"):
        load_paths(tmp_path)


def test_load_paths_invalid_yaml_raises_clear_error(tmp_path):
    _write(tmp_path, "paths.example.yaml", "data: [unclosed")

    with pytest.raises(ConfigError, match="Could not read"):
        load_paths(tmp_path)


def test_load_paths_non_mapping_content_raises_clear_error(tmp_path):
    _write(tmp_path, "paths.example.yaml", "- just\n- a list\n")

    with pytest.raises(ConfigError, match="mapping"):
        load_paths(tmp_path)


def test_load_models_returns_valid_configuration(tmp_path):
    _write(tmp_path, "models.yaml", MODELS_YAML)

    models = load_models(tmp_path)

    assert models["embedding"]["active"] == "bge_m3"
    assert models["bge_m3"]["dimension"] == 1024


def test_load_models_missing_active_model_section_raises_clear_error(tmp_path):
    content = MODELS_YAML.replace("bge_m3:", "other_model:")

    _write(tmp_path, "models.yaml", content)

    with pytest.raises(ConfigError, match="'bge_m3'"):
        load_models(tmp_path)


def test_load_models_invalid_dimension_raises_clear_error(tmp_path):
    content = MODELS_YAML.replace("dimension: 1024", "dimension: 0")

    _write(tmp_path, "models.yaml", content)

    with pytest.raises(ConfigError, match="dimension"):
        load_models(tmp_path)


def test_load_models_non_boolean_normalize_raises_clear_error(tmp_path):
    content = MODELS_YAML.replace("normalize: true", 'normalize: "true"')

    _write(tmp_path, "models.yaml", content)

    with pytest.raises(ConfigError, match="normalize"):
        load_models(tmp_path)


def test_load_models_invalid_batch_size_raises_clear_error(tmp_path):
    content = MODELS_YAML.replace("batch_size: 16", "batch_size: 0")

    _write(tmp_path, "models.yaml", content)

    with pytest.raises(ConfigError, match="batch_size"):
        load_models(tmp_path)


def test_repository_configuration_loads_successfully():
    assert "vector_db" in settings.PATHS
    assert settings.PATHS["project"]["root"] == str(ROOT)
    assert settings.PATHS["logs"]["root"] == str(ROOT / "Logs")
    assert settings.PATHS["prompts"]["root"] == str(ROOT / "Prompts")
    assert settings.MODELS["embedding"]["active"] == "bge_m3"
