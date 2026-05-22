import unsloth  # ต้อง import ก่อนทุก library อื่น
import json
import torch
import gc
from datasets import Dataset
from sentence_transformers import SentenceTransformer
from unsloth import FastLanguageModel
from trl import GRPOTrainer, GRPOConfig

# ==========================================
# 1. โหลด Base Model ด้วย Unsloth (4-bit)
# ==========================================
print("Loading Qwen2.5-3B-Instruct in 4-bit...")
max_seq_length = 2048
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="Qwen/Qwen2.5-3B-Instruct",
    max_seq_length=max_seq_length,
    dtype=None,  # Auto detection
    load_in_4bit=True,  # บังคับ 4-bit เพื่อเซฟ VRAM
)

# ตั้งค่า LoRA และ Gradient Checkpointing
model = FastLanguageModel.get_peft_model(
    model,
    r=32, # เพิ่มจาก 16→32 เพื่อเพิ่มช่องทางเรียนรู้ Pattern ภาษาไทย (ใช้ VRAM คืนจาก paged optimizer)
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
    lora_alpha=32, # รักษา Scaling Ratio alpha/r = 1.0
    lora_dropout=0, # Unsloth แนะนำให้ใช้ 0 สำหรับความเร็วที่เพิ่มขึ้น
    bias="none",
    use_gradient_checkpointing="unsloth", # ฟีเจอร์สำคัญ: ลด VRAM Spike ตอนทำ Backward Pass
    random_state=3407,
    use_rslora=False,
)

# ==========================================
# 2. โหลด Reward Model ลงบน CPU อย่างเดียว
# ==========================================
print("Loading BGE-M3 on CPU strictly to save VRAM...")
# บังคับ device='cpu' ห้ามโหลดลง CUDA เด็ดขาด
bge_m3 = SentenceTransformer('BAAI/bge-m3', device='cpu')

# ==========================================
# 3. Custom Reward Functions
# ==========================================
def format_reward_func(completions, **kwargs):
    """ ให้คะแนนหากโมเดลตอบตามโครงสร้าง CoT 4 ขั้นตอนที่บังคับไว้ """
    rewards = []
    for comp in completions:
        # TRL อาจคืนเป็น list of dict หรือ string เพียวๆ ขึ้นอยู่กับเวอร์ชัน
        text = comp[0]['content'] if isinstance(comp, list) else str(comp)
        score = 0.0
        
        # ตรวจสอบ Format CoT — รองรับทั้ง Numbered List และ Markdown Header
        if "1. บทบาท:" in text or "### บทบาท" in text or "**บทบาท" in text: score += 0.25
        if "2. ประเภทข้อมูล:" in text or "### ประเภทข้อมูล" in text or "**ประเภทข้อมูล" in text: score += 0.25
        if "3. การประเมินกฎหมาย:" in text or "### การประเมินกฎหมาย" in text or "**การประเมิน" in text: score += 0.25
        if "4. คำแนะนำ:" in text or "### คำแนะนำ" in text or "**คำแนะนำ" in text or "**ข้อเสนอแนะ" in text: score += 0.25
        
        rewards.append(score)
    return rewards

def semantic_reward_func(prompts, completions, **kwargs):
    """ ตรวจสอบว่าคำตอบมีความคล้ายคลึงเชิงความหมายกับบริบทหรือไม่ โดยรันบน CPU """
    rewards = []
    for prompt, comp in zip(prompts, completions):
        comp_text = comp[0]['content'] if isinstance(comp, list) else str(comp)
        prompt_text = prompt[0]['content'] if isinstance(prompt, list) else str(prompt)
        
        # ค้นหาด้วย BGE-M3 บน CPU
        with torch.no_grad():
            # ใช้ CPU ล้วน ป้องกันไม่ให้ไปแย่ง VRAM
            emb_comp = bge_m3.encode(comp_text, convert_to_tensor=True, device='cpu')
            emb_prompt = bge_m3.encode(prompt_text, convert_to_tensor=True, device='cpu')
            
            # คำนวณ Cosine Similarity
            sim = torch.nn.functional.cosine_similarity(emb_comp, emb_prompt, dim=0).item()
            # ปรับสเกลคะแนน หากความคล้ายคลึงเกิน 0.5 ถือว่าคะแนนดี
            reward_score = max(0.0, sim) 
            rewards.append(float(reward_score))
            
    # เคลียร์ Cache System RAM
    gc.collect()
    return rewards

# ==========================================
# 4. เตรียมข้อมูล Dataset
# ==========================================
def load_and_format_dataset(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = [json.loads(line) for line in f if line.strip()]
        
    formatted_data = []
    for d in data:
        # แปลงเป็น Format ที่โมเดลเข้าใจ
        prompt_text = f"บริบท:\n{d['context']}\n\nคำถาม: {d['instruction']}"
        formatted_data.append({"prompt": prompt_text})
        
    return Dataset.from_list(formatted_data)

dataset = load_and_format_dataset("pdpa_synthetic_data.jsonl")

# ==========================================
# 5. GRPOTrainer Setup (VRAM Life-Saver)
# ==========================================
training_args = GRPOConfig(
    output_dir="pdpa_qwen_grpo_output",
    learning_rate=3e-6, # ลดจาก 5e-6→3e-6 เพื่อความเสถียรกับ LoRA Rank ที่สูงขึ้น
    per_device_train_batch_size=1, # บังคับทีละ 1 ป้องกัน OOM
    gradient_accumulation_steps=8, # สะสม Gradient ทดแทน Batch Size เล็ก
    max_prompt_length=1536,
    max_completion_length=512, # ขยายจาก 384→512 ให้ CoT ภาษาไทยครบ 4 ขั้นตอน
    num_generations=2, # สร้างคำตอบเปรียบเทียบแค่ 2 ตัวพอ (ปกติ 4-8 แต่ VRAM จะแตก)
    save_steps=100,
    max_steps=400, # เพิ่มจาก 300→400 ให้ LoRA r=32 เรียนรู้ได้เต็มที่
    logging_steps=10,
    report_to="none",
    optim="paged_adamw_8bit" # Swap Optimizer States ไป CPU RAM (ประหยัด VRAM ~0.3-0.5 GB)
)

trainer = GRPOTrainer(
    model=model,
    reward_funcs=[format_reward_func, semantic_reward_func],
    args=training_args,
    train_dataset=dataset,
)

if __name__ == "__main__":
    import os, time as _time
    from datetime import datetime
    print("Starting GRPO Fine-Tuning... (Check your VRAM usage using nvidia-smi)")
    # แจ้งเตือน Unsloth
    import unsloth
    unsloth.FastLanguageModel.for_training(model)
    
    _train_start = _time.time()
    train_result = trainer.train()
    _train_duration = _time.time() - _train_start
    
    print("Training Complete! Saving LoRA Adapters...")
    model.save_pretrained("pdpa_qwen_grpo_lora")
    tokenizer.save_pretrained("pdpa_qwen_grpo_lora")

    # ==========================================
    # บันทึกผลการเทรนลงไฟล์ Log
    # ==========================================
    os.makedirs("logs", exist_ok=True)
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "step": "step3_finetune",
        "status": "SUCCESS",
        "duration_seconds": round(_train_duration, 1),
        "duration_human": f"{_train_duration/3600:.1f} hours",
        "hyperparameters": {
            "lora_rank": 32,
            "lora_alpha": 32,
            "learning_rate": 3e-6,
            "max_steps": 400,
            "batch_size": 1,
            "gradient_accumulation_steps": 8,
            "max_prompt_length": 1536,
            "max_completion_length": 512,
            "num_generations": 2,
            "optimizer": "paged_adamw_8bit",
        },
        "training_metrics": train_result.metrics if hasattr(train_result, 'metrics') else {},
        "dataset_size": len(dataset),
        "output_dir": "pdpa_qwen_grpo_lora",
        "hardware": {},
    }
    # จับ VRAM/RAM
    try:
        if torch.cuda.is_available():
            log_entry["hardware"]["vram_used_mb"] = round(torch.cuda.memory_allocated() / 1024**2, 1)
            log_entry["hardware"]["vram_peak_mb"] = round(torch.cuda.max_memory_allocated() / 1024**2, 1)
    except Exception:
        pass

    import json as _json
    with open("logs/step3_finetune_log.jsonl", "a", encoding="utf-8") as f:
        f.write(_json.dumps(log_entry, ensure_ascii=False, default=str) + "\n")
    print(f"📝 บันทึกผลเทรนลงไฟล์ logs/step3_finetune_log.jsonl")

