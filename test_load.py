from unsloth import FastLanguageModel
print("Downloading and caching Qwen model weights...")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="Qwen/Qwen2.5-3B-Instruct",
    max_seq_length=1024,
    dtype=None,
    load_in_4bit=True,
)
print("Model loaded successfully!")
