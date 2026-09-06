"""CPU-only, same-source CTranslate2 conversion for optional mixed GPUs.

The upstream converter maps the weights. Verify every mapped source tensor
before backend float16 conversion; never infer equivalence from model names.
Heavy dependencies are imported only by the explicit provisioning command.
"""
import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

TRANSFORMERS_REVISION = '052e652d6d53c2b26ffde87e039b723949a53493'
CONVERTER_SHA256 = '7b513a9851d4c05e28dc1bd48703726be3f9c864461dc3c2c1d9e2e08d0e750a'
# Data-only generation settings, pinned separately from all model weights.
GENERATION_CONFIGS = {
    'tiny.en': ('87c7102498dcde7456f24cfd30239ca606ed9063','38744c19d5cede6ff4dab5079c6d6ddc02ca726960bbef208fb602ad5a030eab'),
    'tiny': ('169d4a4341b33bc18d8881c4b69c2e104e1cc0af','a5d5325911f16e74001a72fa13d6e208eee51548f994646de1f4b4cc8b35b512'),
    'base.en': ('911407f4214e0e1d82085af863093ec0b66f9cd6','c5750f05d94777579e00ce26ef65e5d87c108439f90e3ac519df2587b9d5d41f'),
    'base': ('e37978b90ca9030d5170a5c07aadb050351a65bb','444b3f636d2fff89dd9ecf549e2a085b61f7ff0fa0246d4628bac6a3b8cc9ba4'),
    'small.en': ('e8727524f962ee844a7319d92be39ac1bd25655a','aad4e18c0fb1d063c5b38b4080cec3c4c8cdfff95e1de4797013470654a5a72a'),
    'small': ('973afd24965f72e36ca33b3055d56a652f456b4d','71565b8ef50d0bf7a1193ed4bbed195b94e70c18894d81bba2f1233dcec3ab53'),
    'medium.en': ('2e98eb6279edf5095af0c8dedb36bdec0acd172b','6f73a0ac0b2388ebe86bc4430db58a6292cbe835fc50009d5d74e8bb096ef5dc'),
    'medium': ('abdf7c39ab9d0397620ccaea8974cc764cd0953e','503290bc2da3923915d96a96ab96ddaa76e7ea18fbd4c5c1822a2947c82c86db'),
    'large-v1': ('4ef9b41f0d4fe232daafdb5f76bb1dd8b23e01d7','98d27db1dd795a89554115cd2ce9df84076b2df04e463d9ae387a9c12b5e4555'),
    'large-v2': ('ae4642769ce2ad8fc292556ccea8e901f1530655','031721643aab5be7250eb668c6b9b5c67d2549420522ac1291bfd346bfff6297'),
    'large-v3': ('06f233fe06e710322aca913c1bc4249a0d71fce1','fbdfa70135de9b1d31553393f14e80aaeb1936ea36576b2ba864055943c09d23'),
}


def prepare_conversion_assets(directory, model):
    from .device_provisioning import download_verified
    directory = Path(directory)
    converter = directory/'convert_openai_to_hf.py'
    download_verified(f'https://raw.githubusercontent.com/huggingface/transformers/{TRANSFORMERS_REVISION}/src/transformers/models/whisper/convert_openai_to_hf.py',
                      converter, CONVERTER_SHA256, maximum_bytes=1024**2)
    revision, digest = GENERATION_CONFIGS[model]
    name = 'large' if model == 'large-v1' else model
    download_verified(f'https://huggingface.co/openai/whisper-{name}/resolve/{revision}/generation_config.json',
                      directory/'generation_config.json', digest, maximum_bytes=1024**2)


def bind_tokenizer_configuration(config, tokenizer, multilingual):
    """Use verified vocabulary IDs, including 99-language multilingual models.

The pinned upstream converter tests n_vocab > 51865 when selecting special
IDs, but 51865 is itself multilingual. Weight mapping is unaffected. Keep
decoder metadata consistent with the tokenizer built from verified assets.
"""
    end = 50257 if multilingual else 50256
    start = end + 1
    if (len(tokenizer) != config.vocab_size
            or tokenizer.convert_tokens_to_ids('<|endoftext|>') != end
            or tokenizer.convert_tokens_to_ids('<|startoftranscript|>') != start):
        raise ValueError('Converted tokenizer does not match the model vocabulary')
    config.bos_token_id = config.eos_token_id = config.pad_token_id = end
    config.decoder_start_token_id = start
    config.begin_suppress_tokens = [220,end]


def convert(checkpoint, assets, target, model_name):
    from .device_provisioning import CHECKPOINTS, CONVERSION_ASSETS, file_digest
    checkpoint, assets, target = Path(checkpoint), Path(assets), Path(target)
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '':
        raise ValueError('Checkpoint conversion must run in a CPU-only subprocess')
    if model_name not in CHECKPOINTS or file_digest(checkpoint) != CHECKPOINTS[model_name]:
        raise ValueError('Conversion source is not the selected verified checkpoint')
    converter_path = target/'convert_openai_to_hf.py'
    if file_digest(converter_path) != CONVERTER_SHA256:
        raise ValueError('Upstream conversion code failed verification')
    if file_digest(target/'generation_config.json') != GENERATION_CONFIGS[model_name][1]:
        raise ValueError('Generation configuration failed verification')
    for relative in ('whisper/assets/gpt2.tiktoken','whisper/assets/multilingual.tiktoken'):
        if file_digest(assets/relative) != CONVERSION_ASSETS[relative][1]:
            raise ValueError('Tokenizer source failed verification')
    for package, expected in (('transformers','4.57.6'),('tokenizers','0.22.2')):
        if importlib.metadata.version(package) != expected:
            raise ValueError('Install the pinned provisioning requirements: '+package+'=='+expected)
    if (target/'hf').exists() or (target/'ct2').exists():
        raise FileExistsError('Conversion never overwrites a model directory')
    import torch
    import ctranslate2
    from tiktoken.load import load_tiktoken_bpe
    from transformers import GenerationConfig, WhisperFeatureExtractor, WhisperProcessor, WhisperTokenizerFast
    torch.set_num_threads(2)
    spec = importlib.util.spec_from_file_location('subgen_verified_upstream_converter',converter_path)
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    generation = GenerationConfig.from_dict(json.loads((target/'generation_config.json').read_text()))
    converter._get_generation_config = lambda *args, **kwargs: generation
    converter._TOKENIZERS.update(english=str(assets/'whisper/assets/gpt2.tiktoken'),
                                 multilingual=str(assets/'whisper/assets/multilingual.tiktoken'))
    converter.load_tiktoken_bpe = load_tiktoken_bpe
    model, multilingual, languages = converter.convert_openai_whisper_to_tfms(str(checkpoint),str(target/'hf'))
    if multilingual != (not model_name.endswith('.en')):
        raise ValueError('Converted model language capability contradicts its source')
    original = torch.load(checkpoint,map_location='cpu',weights_only=True)['model_state_dict']
    converter.remove_ignore_keys_(original)
    converter.rename_keys(original)
    converted = model.model.state_dict()
    if set(original) != set(converted) or not all(torch.equal(v,converted[k].to(v.dtype)) for k,v in original.items()):
        raise ValueError('Converted tensors do not exactly match the source checkpoint')
    tensor_count = len(original)
    del original, converted
    tokenizer = converter.convert_tiktoken_to_hf(multilingual,languages)
    extractor = WhisperFeatureExtractor(feature_size=model.config.num_mel_bins)
    WhisperProcessor(tokenizer=tokenizer,feature_extractor=extractor).save_pretrained(target/'hf')
    fast = WhisperTokenizerFast.from_pretrained(target/'hf',local_files_only=True)
    fast.save_pretrained(target/'hf',legacy_format=False)
    bind_tokenizer_configuration(model.config,fast,multilingual)
    model.half().save_pretrained(target/'hf')
    del model
    ctranslate2.converters.TransformersConverter(str(target/'hf'),
        copy_files=['tokenizer.json','preprocessor_config.json'],trust_remote_code=False).convert(
            str(target/'ct2'),quantization='float16')
    if file_digest(checkpoint) != CHECKPOINTS[model_name]:
        raise ValueError('Checkpoint changed during conversion')
    with (target/'source-verification.json').open('x',encoding='utf8') as stream:
        json.dump(dict(source_checkpoint_sha256=CHECKPOINTS[model_name],source_tensors_verified=tensor_count,
                       converter_revision=TRANSFORMERS_REVISION,precision='float16'),stream,indent=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('checkpoint','assets','target','model'):
        parser.add_argument('--'+name,required=True)
    args = parser.parse_args()
    convert(args.checkpoint,args.assets,args.target,args.model)
