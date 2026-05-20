import pdfplumber
import re
import json
from pythainlp.tokenize import word_tokenize, sent_tokenize

def extract_text_from_pdf(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def clean_text(text):
    # ลบ Header/Footer ราชกิจจานุเบกษาทิ้ง เพื่อไม่ให้รบกวนข้อความกฎหมาย
    footer_pattern = re.compile(r'หน้า\s*[๐-๙0-9]+\s*เล่ม\s*[๐-๙0-9]+\s*ตอนที่\s*[๐-๙0-9]+\s*[ก-ฮ]?\s*ราชกิจจานุเบกษา\s*[๐-๙0-9]+\s*[ก-ฮา-์]+\s*[๐-๙0-9]+')
    text = footer_pattern.sub(' ', text)
    lines = text.split('\n')
    return [line.strip() for line in lines if line.strip()]

def process_thai_text(text):
    sentences = sent_tokenize(text, engine="crfcut")
    return " ".join([s.strip() for s in sentences if s.strip()])

def hierarchy_aware_chunking(lines, law_name="PDPA"):
    chunks = []
    current_chapter = "ไม่ระบุ"
    current_section = None
    current_chunk_lines = []
    
    # จับโครงสร้าง หมวด และ มาตรา
    chapter_pattern = re.compile(r'^หมวด(?:ที่)?\s*([๐-๙0-9]+)')
    section_pattern = re.compile(r'^มาตรา\s*([๐-๙0-9]+(?:/[๐-๙0-9]+)?)')
    
    for line in lines:
        chapter_match = chapter_pattern.match(line)
        section_match = section_pattern.match(line)
        
        if chapter_match:
            current_chapter = chapter_match.group(1)
            continue
            
        if section_match:
            if current_section and current_chunk_lines:
                raw_text = " ".join(current_chunk_lines)
                chunks.append({
                    "metadata": {"law": law_name, "chapter": current_chapter, "section": current_section},
                    "text": process_thai_text(raw_text)
                })
            current_section = section_match.group(1)
            current_chunk_lines = [line]
        else:
            if current_section:
                current_chunk_lines.append(line)
                
    if current_section and current_chunk_lines:
        raw_text = " ".join(current_chunk_lines)
        chunks.append({
            "metadata": {"law": law_name, "chapter": current_chapter, "section": current_section},
            "text": process_thai_text(raw_text)
        })
        
    return chunks

if __name__ == "__main__":
    pdf_path = "PDPA.pdf"
    output_json_path = "pdpa_structured_chunks.json"

    print("1. กำลังสกัดข้อความจากเอกสาร PDF...")
    raw_text = extract_text_from_pdf(pdf_path)
    print("2. กำลังทำความสะอาดข้อความเบื้องต้น...")
    lines = clean_text(raw_text)
    print("3. ดำเนินการ Hierarchy-Aware Chunking...")
    legal_chunks = hierarchy_aware_chunking(lines)

    print("4. กำลังบันทึกผลลัพธ์เป็นไฟล์ JSON...")
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(legal_chunks, f, ensure_ascii=False, indent=4)

    print(f"✨ ทำการสร้าง Chunk สำเร็จทั้งหมด {len(legal_chunks)} มาตรา")
