# ⚖️ PDPA Legal AI Assistant (ระบบผู้ช่วยกฎหมายคุ้มครองข้อมูลส่วนบุคคลอัจฉริยะ)

ระบบผู้ช่วยปัญญาประดิษฐ์ด้านกฎหมายคุ้มครองข้อมูลส่วนบุคคล (PDPA) ของประเทศไทยสำหรับการใช้งานระดับองค์กร ถูกออกแบบและพัฒนาภายใต้ข้อจำกัดทางฮาร์ดแวร์ระดับเริ่มต้น (**GPU RTX 3060 VRAM 6GB + RAM 16GB**) และประมวลผลข้อมูลจากเอกสารกฎหมายต้นฉบับเพียงไฟล์เดียว (**PDPA.pdf**)

ระบบนี้ใช้สถาปัตยกรรมแบบบูรณาการผ่านกระบวนการ 6 ขั้นตอน เพื่อให้สามารถทำงานได้จริง รวดเร็ว มีความแม่นยำทางข้อกฎหมาย และปลอดภัยต่อข้อมูลส่วนบุคคลขั้นสูง

---

## 🏗️ โครงสร้างสถาปัตยกรรมระบบ (System Architecture)

```
[ PDF กฎหมายต้นฉบับ ]
       │
       ▼ (Step 1: Hierarchy-Aware Chunking)
[ Structured Chunks ] ──(Step 2: SDG via Gemini API)──▶ [ Synthetic Dataset ]
       │                                                          │
       │                                                          ▼ (Step 3: PEFT/GRPO)
       │                                                  [ Qwen2.5-3B Fine-Tuned ]
       ▼
[ Vector Store (ChromaDB) ] ◄──(Step 4: BGE-M3 Embeddings on CPU)
       │
       ├─▶ [ NitiLink Engine ] (ดึงบริบทเชื่อมโยงมาตราอัตโนมัติ)
       │
       ▼ (Step 5: Guardrails Pipeline on CPU)
[ User Query ] ──▶ [ PII Redaction ] ──▶ [ Topic Guard ] ──▶ [ LLM Inference ] ──▶ [ User Response ]
```

---

## 📂 รายละเอียดขั้นตอนวิศวกรรมการพัฒนา (Engineering Steps)

### 📌 Step 1: Hierarchy-Aware Chunking (`step1_chunking.py`)
ทำหน้าที่สกัดและทำความสะอาดข้อความจากไฟล์ `PDPA.pdf` โดยใช้กลไกการตัดคำภาษาไทยของ **PyThaiNLP** 
* **จุดเด่น:** ใช้โครงสร้างแบบ **Hierarchy-Aware** โดยสกัดและระบุหมวดหมู่ ส่วน และมาตราอ้างอิงให้ติดไปกับทุก Chunk เพื่อป้องกันไม่ให้ข้อมูลกฎหมายขาดบริบทหลักเมื่อถูกแบ่งเป็นชิ้นย่อย

### 📌 Step 2: Synthetic Data Generation (`step2_sdg.py`)
สร้างชุดข้อมูลคำถาม-คำตอบจำลองคุณภาพสูงด้วย **Gemini API** จากเอกสารกฎหมายที่เราแบ่งไว้ใน Step 1
* **จุดเด่น:** บังคับโครงสร้างคำตอบในรูปแบบ **Legal Chain-of-Thought (Legal CoT)** ซึ่งจำลองการคิดเป็นขั้นตอนของนักกฎหมายจริง (ระบุบทบาท -> ประเภทข้อมูล -> ข้อกฎหมาย -> คำแนะนำแก่ผู้ใช้)

### 📌 Step 3: Parameter-Efficient Fine-Tuning & GRPO (`step3_finetune.py`)
นำชุดข้อมูลสังเคราะห์จาก Step 2 มาพัฒนาต่อยอดโมเดลภาษาขนาดเล็ก **Qwen2.5-3B-Instruct** (โหลดแบบ 4-bit) 
* **จุดเด่น:** ใช้เทคนิค **QLoRA** เพื่อประหยัดหน่วยความจำบน VRAM 6GB และใช้เทคนิค **GRPO (Generative Reward Policy Optimization)** ในการทำ Reinforcement Learning (RL) ปรับตรรกะและให้รางวัลโมเดลเมื่อตอบตรงประเด็นและระบุเลขมาตรากฎหมายถูกต้อง

### 📌 Step 4: Hybrid RAG & NitiLink Engine (`step4_rag.py`)
ระบบสืบค้นและเสริมบริบทให้แก่ AI โดยทำงานสแกนบน **CPU (0 VRAM Cost)** 
* **จุดเด่น:** มีกลไก **NitiLink Engine** ซึ่งใช้ RegEx จับมาตราที่เชื่อมโยงกัน (Cross-referencing) เช่น หากคำถามดึงบริบทมาตรา 24 ซึ่งระบุข้อยกเว้นไว้ ระบบจะดึงข้อมูลมาตรา 26 (ข้อมูลอ่อนไหว) ขึ้นมาเสริมให้อัตโนมัติ ป้องกันปัญหาการตอบผิดพลาด

### 📌 Step 5: Multi-Stage Guardrails (`step5_guardrails.py`)
สร้างเกราะป้องกันข้อมูลรั่วไหลและการใช้งานผิดประเภทแบบ 3 ชั้น ทำงานบน CPU ทั้งหมด
* **ชั้นที่ 1 (PII Redactor):** ตรวจจับข้อมูลส่วนบุคคลของไทยด้วย Microsoft Presidio และพัฒนาตัวรับรู้ส่วนบุคคลเชิงคำนวณ (**Modulo-11 Checksum**) เพื่อจับและเซ็นเซอร์เลขบัตรประชาชนไทย เบอร์โทรศัพท์ และอีเมลออกทันที
* **ชั้นที่ 2 (Topic Guard):** ตรวจสอบและบล็อกการพยายามแฮกข้อมูล (Jailbreak) หรือคำถามที่ไม่เกี่ยวข้องกับกฎหมาย
* **ชั้นที่ 3 (Output Filter):** ตรวจสอบความถูกต้องและคำหยาบคายก่อนตอบกลับ

### 📌 Step 6: Evaluation Framework (`step6_evaluation.py`)
กรอบทดสอบประสิทธิภาพระบบเพื่อตรวจสอบคุณภาพก่อนใช้งานจริง (Production) ประกอบด้วย:
* **Retrieval Evaluation:** วัดค่า **MultiMRR** และ Recall เพื่อประเมินความแม่นยำในการดึงมาตรากฎหมาย
* **LLM-as-a-Judge:** ใช้ Gemini API ประเมินค่าความขัดแย้งของข้อมูล (Contradiction) และความครอบคลุม (Semantic Coverage)
* **Safety Red Teaming:** ทดสอบความแข็งแกร่งด้วย **ThaiSafetyBench**

---

## 🎨 หน้าจอการใช้งาน Streamlit User Interface (`app.py`)
เราได้จัดทำแอปพลิเคชันหน้าจอแชทบอทให้สามารถทดสอบเล่นได้ทันทีแบบ Interactive
* **ฟีเจอร์เด่น:**
  - แถบ System Console ด้านข้างจำลองการทำงานของฮาร์ดแวร์ VRAM และระบบหลังบ้าน
  - การโชว์สถานะ Pipeline การประมวลผลเป็นระยะอย่างละเอียด
  - แสดง Citations (เอกสารอ้างอิงมาตรากฎหมาย) ในกล่องคำตอบแบบพับเก็บได้
  - ปุ่มยิงคำถามตัวอย่างอย่างรวดเร็ว (Quick Prompts)

---

## 🚀 วิธีการติดตั้งและการรันระบบ (Setup & Running)

1. **ดาวน์โหลดและเตรียม Conda Environment:**
   ```bash
   conda activate NSC
   ```

2. **ติดตั้งไลบรารีที่จำเป็น:**
   ```bash
   pip install streamlit datasets pythainlp sentence-transformers chromadb
   ```

3. **สั่งรันหน้า UI ของผู้ช่วยกฎหมาย:**
   ```bash
   streamlit run app.py
   ```
   จากนั้นเข้าใช้งานได้ผ่านบราวเซอร์ที่: `http://localhost:8501`

---

## 🔒 มาตรการการจัดการทรัพยากร (6GB VRAM Optimization)
ระบบนี้สามารถรันได้อย่างเสถียรบนสเปก RTX 3060 (6GB VRAM) เนื่องจากใช้โครงสร้างแยกการประมวลผล (Isolated Processing Architecture):
* **GPU (รันเฉพาะการสร้างคำตอบ):** Qwen2.5-3B ถูกบีบอัดให้อยู่ในรูป 4-bit (ใช้ VRAM ~2.5GB) 
* **CPU (รันงานสืบค้นและสแกน):** งานฝังเวกเตอร์ (BGE-M3), ฐานข้อมูล ChromaDB, ระบบสแกนคำหยาบ และ PII Redactor ถูกควบคุมให้รันบนหน่วยประมวลผลหลัก (CPU) ทั้งหมด ส่งผลให้ VRAM เหลือพื้นที่สำหรับประมวลผล Token ยาวๆ อย่างเพียงพอ
