"""Provisioning binds every model variant to its actual tokenizer/checkpoint."""
from types import SimpleNamespace
import pytest
from subgen_core import checkpoint_conversion as c
from subgen_core.device_provisioning import CHECKPOINTS


@pytest.mark.parametrize('vocabulary,multilingual,end',[(51864,False,50256),(51865,True,50257),(51866,True,50257)])
def test_special_ids_match_all_whisper_vocabulary_generations(vocabulary,multilingual,end):
    class Tokenizer:
        def __len__(self):return vocabulary
        def convert_tokens_to_ids(self,name):return {'<|endoftext|>':end,'<|startoftranscript|>':end+1}[name]
    config=SimpleNamespace(vocab_size=vocabulary,decoder_start_token_id=50257,eos_token_id=50256)
    c.bind_tokenizer_configuration(config,Tokenizer(),multilingual)
    assert config.decoder_start_token_id == end+1
    assert config.eos_token_id == config.bos_token_id == config.pad_token_id == end
    assert config.begin_suppress_tokens == [220,end]


def test_wrong_tokenizer_is_not_repaired_into_acceptance():
    class Tokenizer:
        def __len__(self):return 51865
        def convert_tokens_to_ids(self,_):return 999
    config=SimpleNamespace(vocab_size=51865,decoder_start_token_id=123)
    with pytest.raises(ValueError,match='vocabulary'):
        c.bind_tokenizer_configuration(config,Tokenizer(),True)
    assert config.decoder_start_token_id == 123


def test_all_public_models_have_pinned_generation_data():
    import re
    assert set(c.GENERATION_CONFIGS) == set(CHECKPOINTS)
    for revision,digest in c.GENERATION_CONFIGS.values():
        assert re.fullmatch('[a-f0-9]{40}',revision)
        assert re.fullmatch('[a-f0-9]{64}',digest)


def test_conversion_refuses_gpu_visible_environment_before_import(tmp_path,monkeypatch):
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES',raising=False)
    with pytest.raises(ValueError,match='CPU-only'):
        c.convert(tmp_path/'absent.pt',tmp_path,tmp_path,'base')
