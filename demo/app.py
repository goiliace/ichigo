import gradio as gr
import torch
import torchaudio
from encodec import EncodecModel
from whisperspeech.vq_stoks import RQBottleneckTransformer
from encodec.utils import convert_audio
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, pipeline
from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer
from threading import Thread
import logging
import os
from generate_audio import (
    TTSProcessor,
)  
import uuid

device = "cuda"
vq_model = RQBottleneckTransformer.load_model(
        "whisper-vq-stoks-medium-en+pl-fixed.model"
    ).to(device)
vq_model.ensure_whisper(device)
tts = TTSProcessor(device)
use_8bit = False    
llm_path = "homebrewltd/Llama3.1-s-instruct-2024-08-19-epoch-3"
tokenizer = AutoTokenizer.from_pretrained(llm_path)
model_kwargs = {}
if use_8bit:
    model_kwargs["quantization_config"] = BitsAndBytesConfig(
        load_in_8bit=True,
        llm_int8_enable_fp32_cpu_offload=False,
        llm_int8_has_fp16_weight=False,
    )
else:
    model_kwargs["torch_dtype"] = torch.bfloat16
model = AutoModelForCausalLM.from_pretrained(llm_path, **model_kwargs).to(device)

def audio_to_sound_tokens_whisperspeech(audio_path):
    
    wav, sr = torchaudio.load(audio_path)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    with torch.no_grad():
        codes = vq_model.encode_audio(wav.to(device))
        codes = codes[0].cpu().tolist()
    
    result = ''.join(f'<|sound_{num:04d}|>' for num in codes)
    return f'<|sound_start|>{result}<|sound_end|>'
def audio_to_sound_tokens_whisperspeech_transcribe(audio_path):
    
    wav, sr = torchaudio.load(audio_path)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    with torch.no_grad():
        codes = vq_model.encode_audio(wav.to(device))
        codes = codes[0].cpu().tolist()
    
    result = ''.join(f'<|sound_{num:04d}|>' for num in codes)
    return f'<|reserved_special_token_69|><|sound_start|>{result}<|sound_end|>'
def audio_to_sound_tokens(audio_path, target_bandwidth=1.5, device="cuda"):
    model = EncodecModel.encodec_model_24khz()
    model.set_target_bandwidth(target_bandwidth)
    model.to(device)
    
    wav, sr = torchaudio.load(audio_path)
    wav = convert_audio(wav, sr, model.sample_rate, model.channels)
    wav = wav.unsqueeze(0).to(device)
    
    with torch.no_grad():
        encoded_frames = model.encode(wav)
        codes = torch.cat([encoded[0] for encoded in encoded_frames], dim=-1)
    
    audio_code1, audio_code2 = codes[0][0], codes[0][1]
    flatten_tokens = torch.stack((audio_code1, audio_code2), dim=1).flatten().tolist()
    result = ''.join(f'<|sound_{num:04d}|>' for num in flatten_tokens)
    return f'<|sound_start|>{result}<|sound_end|>'

def setup_pipeline(model_path, use_4bit=False, use_8bit=False):
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model_kwargs = {"device_map": "auto"}
    if use_8bit:
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_8bit=True,
            llm_int8_enable_fp32_cpu_offload=False,
            llm_int8_has_fp16_weight=False,
        )
    else:
        model_kwargs["torch_dtype"] = torch.bfloat16
    model = AutoModelForCausalLM.from_pretrained(model_path, **model_kwargs)
    return pipeline("text-generation", model=model, tokenizer=tokenizer)

def text_to_audio_file(text):
    # gen a random id for the audio file
    id = str(uuid.uuid4())
    temp_file = f"./user_audio/{id}_temp_audio.wav"
    text = text
    text_split = "_".join(text.lower().split(" "))  
    # remove the last character if it is a period
    if text_split[-1] == ".":
        text_split = text_split[:-1]
    tts.convert_text_to_audio_file(text, temp_file)
    print(f"Saved audio to {temp_file}")
    return temp_file
def process_input(input_type, text_input=None, audio_file=None):
    
    for partial_message in process_audio(audio_file):
        yield partial_message
    
def process_transcribe_input(input_type, text_input=None, audio_file=None):
    
    for partial_message in process_audio(audio_file, transcript=True):
        yield partial_message
    
class StopOnTokens(StoppingCriteria):
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> bool:
        # encode </s> token
        stop_ids = [tokenizer.eos_token_id]  # Adjust this based on your model's tokenizer
        for stop_id in stop_ids:
            if input_ids[0][-1] == stop_id:
                return True
        return False
def process_audio(audio_file, transcript=False):
    if audio_file is None:
            raise ValueError("No audio file provided")

    logging.info(f"Audio file received: {audio_file}")
    logging.info(f"Audio file type: {type(audio_file)}")

    sound_tokens = audio_to_sound_tokens_whisperspeech_transcribe(audio_file)  if transcript else audio_to_sound_tokens_whisperspeech(audio_file)
    logging.info("Sound tokens generated successfully")
    # logging.info(f"audio_file: {audio_file.name}")
    messages = [
        {"role": "user", "content": sound_tokens},
    ]

    stop = StopOnTokens()
    input_ids = tokenizer.encode(tokenizer.apply_chat_template(messages, tokenize=False), return_tensors="pt")
    input_ids = input_ids.to(model.device)

    streamer = TextIteratorStreamer(tokenizer, timeout=10., skip_prompt=True, skip_special_tokens=True)  
    generation_kwargs = dict(
        input_ids=input_ids,
        streamer=streamer,
        max_new_tokens=1024,
        do_sample=False,
        stopping_criteria=StoppingCriteriaList([stop])
    )

    thread = Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()

    partial_message = ""
    for new_token in streamer:
        partial_message += new_token
        if tokenizer.eos_token in partial_message:
            break
        partial_message = partial_message.replace("assistant\n\n", "")
        yield partial_message

good_examples = []
for file in os.listdir("./examples"):
    if file.endswith(".wav"):
        good_examples.append([f"./examples/{file}"])
bad_examples = []
for file in os.listdir("./bad_examples"):
    if file.endswith(".wav"):
        bad_examples.append([f"./bad_examples/{file}"])
examples = []
examples.extend(good_examples)
examples.extend(bad_examples)

with gr.Blocks() as iface:
    gr.Markdown("# Llama3-1-S: checkpoint Aug 20, 2024")
    gr.Markdown("Enter text to convert to audio, then submit the audio to generate text or Upload Audio")
    
    with gr.Row():
        input_type = gr.Radio(["text", "audio"], label="Input Type", value="audio")
        text_input = gr.Textbox(label="Text Input", visible=False)
        audio_input = gr.Audio(label="Audio", type="filepath", visible=True)
    
    convert_button = gr.Button("Convert to Audio", visible=False)
    submit_button = gr.Button("Submit for Processing")
    transcrip_button = gr.Button("Please Transcribe the audio for me")
    
    text_output = gr.Textbox(label="Generated Text")
    
    def update_visibility(input_type):
        return (gr.update(visible=input_type == "text"), 
                gr.update(visible=input_type == "text"))
    def convert_and_display(text):
        audio_file = text_to_audio_file(text)
        return audio_file
    def process_example(file_path):
        return update_visibility("audio") 

    input_type.change(
        update_visibility,
        inputs=[input_type],
        outputs=[text_input, convert_button]
    )

    convert_button.click(
        convert_and_display,
        inputs=[text_input],
        outputs=[audio_input]
    )
    
    submit_button.click(
        process_input,
        inputs=[input_type, text_input, audio_input],
        outputs=[text_output]
    )
    transcrip_button.click(
        process_transcribe_input,
        inputs=[input_type, text_input, audio_input],
        outputs=[text_output]
    )
    
    gr.Examples(examples, inputs=[audio_input], outputs=[audio_input], fn=process_example)
iface.queue(max_size=10)
# iface.launch(server_name="127.0.0.1", server_port=8080)
# launch locally
iface.launch(server_name="0.0.0.0")
