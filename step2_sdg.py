import json
import time
import os
import google.generativeai as genai

# ==========================================
# 1. ตั้งค่า API Key และโมเดล
# ==========================================
# พยายามโหลดจาก Environment Variable ถ้าไม่มีให้ใช้ Key เดิมที่คุณตั้งไว้
API_KEY = os.environ.get('GEMINI_API_KEY', 'AIzaSyBgKUgCaPi36I3mXRJhmsP4cFuV1fD1U_Q')
genai.configure(api_key=API_KEY)

# อัปเดตเป็นรุ่น gemini-3-flash-preview หรือรุ่นอื่นๆ ที่คุณต้องการ
model = genai.GenerativeModel('gemini-3-flash-preview')

SYSTEM_PROMPT = """
คุณคือ "ผู้เชี่ยวชาญด้านกฎหมายคุ้มครองข้อมูลส่วนบุคคล (PDPA)"
เป้าหมายของคุณคือการสร้างชุดข้อมูลฝึกสอน (Training Data) คุณภาพสูงจำนวน 2-3 คู่ จากบริบท (Context) ที่กำหนดให้เท่านั้น ห้ามคิดข้อกำหนดกฎหมายขึ้นเองเด็ดขาด

[คำสั่งในการสร้างคำถาม (Instruction Generation)]
ให้สร้างคำถามหลากหลายรูปแบบ ครอบคลุมทั้ง:
- คำถามเชิงข้อเท็จจริง (Fact-based)
- คำถามเชิงสถานการณ์สมมติระดับองค์กร (Scenario-based)
- คำถามเชิงซับซ้อนที่ต้องอาศัยการตีความข้อยกเว้น (Complex Application)

[คำสั่งในการสร้างคำตอบ (Response Synthesis & Legal CoT)]
คำตอบของคุณ **ต้อง** แสดงกระบวนการคิดวิเคราะห์แบบนิรนัย (Deductive Reasoning) ตาม 4 ขั้นตอนนี้อย่างเคร่งครัด:
1. บทบาท: ระบุบทบาทของบุคคล/นิติบุคคล (เช่น ผู้ควบคุมข้อมูลส่วนบุคคล หรือ ผู้ประมวลผลข้อมูลส่วนบุคคล)
2. ประเภทข้อมูล: จำแนกประเภทของข้อมูล (ข้อมูลส่วนบุคคลทั่วไป หรือ ข้อมูลส่วนบุคคลที่มีความอ่อนไหว/Sensitive Data)
3. การประเมินกฎหมาย: ประเมินฐานความชอบด้วยกฎหมายและข้อยกเว้น ภายใต้มาตราที่เกี่ยวข้องในบริบท
4. คำแนะนำ: สรุปคำแนะนำหรือคำสั่งในการปฏิบัติตามกฎหมายที่ชัดเจน

[รูปแบบผลลัพธ์ (Output Format)]
ให้ส่งคืนผลลัพธ์เป็น JSON Array ล้วนๆ ในรูปแบบ:
{"qa_pairs": [
  {
    "instruction": "คำถาม...",
    "response": "1. บทบาท: ...\\n2. ประเภทข้อมูล: ...\\n3. การประเมินกฎหมาย: ...\\n4. คำแนะนำ: ..."
  }
]}
"""

def generate_synthetic_data(chunk: dict, max_retries: int = 3) -> list:
    context = f"หมวด: {chunk['metadata']['chapter']}\nมาตรา: {chunk['metadata']['section']}\nเนื้อหากฎหมาย:\n{chunk['text']}"
    
    # ตั้งค่าให้ Gemini คืนค่าเป็น JSON
    generation_config = genai.GenerationConfig(
        response_mime_type="application/json",
        temperature=0.7
    )
    
    for attempt in range(max_retries):
        try:
            prompt = f"{SYSTEM_PROMPT}\n\nบริบทตั้งต้น:\n{context}\n\nจงสร้างข้อมูล QA Pairs ตามคำสั่ง"
            response = model.generate_content(
                prompt,
                generation_config=generation_config
            )
            
            # แกะข้อมูล JSON ที่โมเดลตอบกลับมา
            result_str = response.text
            parsed_json = json.loads(result_str)
            qa_pairs = parsed_json.get('qa_pairs', [])
            
            formatted_data = []
            for qa in qa_pairs:
                formatted_data.append({
                    "instruction": qa["instruction"],
                    "context": context,
                    "response": qa["response"]
                })
            return formatted_data
            
        except Exception as e:
            error_str = str(e)
            print(f"เกิดข้อผิดพลาดที่มาตรา {chunk['metadata']['section']} (ครั้งที่ {attempt+1}): {error_str.split('[')[0].strip()}")
            if "429" in error_str or "Quota exceeded" in error_str:
                wait_time = 15 * (attempt + 1)
                print(f"-> ติด Rate Limit (Free Tier) รอก่อน {wait_time} วินาที...")
                time.sleep(wait_time)
            else:
                time.sleep(3)
            
    return []

def run_sdg_pipeline(input_json: str, output_jsonl: str, sample_size: int = None):
    with open(input_json, 'r', encoding='utf-8') as f:
        chunks = json.load(f)
        
    # จำกัดจำนวนในการสร้างเพื่อทดสอบ Pipeline
    target_chunks = chunks[:sample_size] if sample_size else chunks
    print(f"เริ่มกระบวนการ SDG จากข้อมูล {len(target_chunks)} มาตรา...")
    
    with open(output_jsonl, 'w', encoding='utf-8') as f:
        for i, chunk in enumerate(target_chunks):
            print(f"กำลังประมวลผล มาตรา {chunk['metadata']['section']} ({i+1}/{len(target_chunks)})...")
            generated_pairs = generate_synthetic_data(chunk)
            
            for pair in generated_pairs:
                f.write(json.dumps(pair, ensure_ascii=False) + '\n')
                
            # หน่วงเวลา 12 วินาทีระหว่างมาตรา เพื่อไม่ให้เกิน 5 Requests/Minute (Free Tier)
            if i < len(target_chunks) - 1:
                time.sleep(12)
                
    print(f"\n✅ สร้างข้อมูลสังเคราะห์สำเร็จ บันทึกลงไฟล์ '{output_jsonl}'")

if __name__ == "__main__":
    # รันกับข้อมูลทั้งหมด (Full Process)
    run_sdg_pipeline("pdpa_structured_chunks.json", "pdpa_synthetic_data.jsonl", sample_size=None)
