"""
Step 4: RAG Pipeline สำหรับ AI ผู้เชี่ยวชาญกฎหมาย PDPA
==========================================================
สถาปัตยกรรม:
  [User Query]
       │
       ▼
  ┌─────────────┐     ┌──────────────────┐
  │  BGE-M3     │────▶│  ChromaDB        │
  │  (CPU only) │     │  (Vector Store)  │
  └─────────────┘     └────────┬─────────┘
                               │ Top-K Chunks
                               ▼
                    ┌──────────────────────┐
                    │  NitiLink Engine     │
                    │  (Cross-referencing) │
                    └────────┬─────────────┘
                             │ Enriched Context
                             ▼
                    ┌──────────────────┐
                    │  Qwen2.5-3B      │
                    │  + LoRA (4-bit)  │
                    │  (GPU CUDA)      │
                    └────────┬─────────┘
                             │
                             ▼
                      [Legal CoT Answer]

VRAM Budget (6GB):
  - Qwen 3B 4-bit  ≈ 2.5 GB
  - KV Cache        ≈ 0.5-1 GB
  - BGE-M3         → CPU (0 GB VRAM)
  - ChromaDB       → CPU/Disk (0 GB VRAM)
  - Headroom       ≈ 2-3 GB
"""

import unsloth  # ต้อง import ก่อนทุก library อื่นๆ
import json
import re
import os
import gc
import torch
import chromadb
from sentence_transformers import SentenceTransformer

# ==========================================
# 1. Configuration
# ==========================================
CHUNKS_PATH = "pdpa_structured_chunks.json"
CHROMA_DIR = "pdpa_vectordb"
LORA_PATH = "pdpa_qwen_grpo_lora"
BASE_MODEL = "Qwen/Qwen2.5-3B-Instruct"
EMBEDDING_MODEL = "BAAI/bge-m3"
TOP_K = 3  # จำนวน Chunk ที่ดึงมาจาก Vector DB (ลดลงเพื่อไม่ให้ Prompt ยาวเกินไป)

# ==========================================
# 2. โหลด BGE-M3 บน CPU อย่างเดียว
# ==========================================
print("📦 Loading BGE-M3 embedding model on CPU...")
embedder = SentenceTransformer(EMBEDDING_MODEL, device="cpu")

# ==========================================
# 3. สร้าง / โหลด ChromaDB Vector Store
# ==========================================
def build_vector_store(chunks_path: str, chroma_dir: str, force_rebuild: bool = False):
    """สร้าง Vector Database จากไฟล์ Chunks JSON"""
    client = chromadb.PersistentClient(path=chroma_dir)

    # ตรวจสอบว่ามี Collection อยู่แล้วหรือยัง
    existing = [c.name for c in client.list_collections()]
    if "pdpa_sections" in existing and not force_rebuild:
        print("✅ Vector DB พบข้อมูลเดิมอยู่แล้ว ข้ามขั้นตอนการสร้าง")
        return client.get_collection("pdpa_sections")

    # ลบ Collection เก่า (ถ้ามี) แล้วสร้างใหม่
    if "pdpa_sections" in existing:
        client.delete_collection("pdpa_sections")

    collection = client.create_collection(
        name="pdpa_sections",
        metadata={"hnsw:space": "cosine"}
    )

    with open(chunks_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    print(f"📊 กำลังสร้าง Embeddings สำหรับ {len(chunks)} มาตรา...")

    # Batch encode เพื่อความเร็ว
    texts = [c["text"] for c in chunks]
    embeddings = embedder.encode(texts, show_progress_bar=True, batch_size=16).tolist()

    # เตรียมข้อมูลสำหรับ ChromaDB
    ids = []
    documents = []
    metadatas = []

    for i, chunk in enumerate(chunks):
        section_id = chunk["metadata"]["section"]
        ids.append(f"section_{section_id}_{i}")
        documents.append(chunk["text"])
        metadatas.append({
            "law": chunk["metadata"]["law"],
            "chapter": chunk["metadata"]["chapter"],
            "section": section_id
        })

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas
    )

    print(f"✅ สร้าง Vector DB สำเร็จ: {collection.count()} รายการ")
    return collection


# ==========================================
# 4. NitiLink Engine (Cross-referencing)
# ==========================================
def extract_referenced_sections(text: str) -> list[str]:
    """
    สกัดเลขมาตราที่ถูกอ้างอิงในข้อความ
    ตัวอย่าง: "ตามมาตรา ๒๔" -> ["๒๔"]
             "มาตรา ๒๖ (๑)" -> ["๒๖"]
             "มาตรา ๙๕ และมาตรา ๙๖" -> ["๙๕", "๙๖"]
    """
    # จับทั้งเลขไทยและเลขอารบิก
    pattern = r'มาตรา\s*([๐-๙0-9]+(?:/[๐-๙0-9]+)?)'
    matches = re.findall(pattern, text)
    return list(set(matches))


def nitilink_enrich(primary_chunks: list[dict], collection) -> list[dict]:
    """
    NitiLink Logic: ตรวจสอบว่า Chunks ที่ดึงมามีการอ้างอิงถึงมาตราอื่นหรือไม่
    ถ้ามี ให้ดึงมาตราที่ถูกอ้างอิงนั้นเข้ามาเติมเต็มบริบทโดยอัตโนมัติ
    """
    # เก็บ Section ที่มีอยู่แล้ว เพื่อไม่ให้ดึงซ้ำ
    existing_sections = set()
    for chunk in primary_chunks:
        if chunk.get("metadata"):
            existing_sections.add(chunk["metadata"].get("section", ""))

    enriched = list(primary_chunks)  # copy
    referenced_sections_to_fetch = set()

    # สแกนทุก Chunk เพื่อหามาตราที่ถูกอ้างอิง
    for chunk in primary_chunks:
        text = chunk.get("document", chunk.get("text", ""))
        refs = extract_referenced_sections(text)
        for ref in refs:
            if ref not in existing_sections:
                referenced_sections_to_fetch.add(ref)

    # จำกัดไม่ให้ดึง Cross-reference เกิน 3 มาตรา เพื่อป้องกัน Prompt ยาวเกิน
    if len(referenced_sections_to_fetch) > 3:
        referenced_sections_to_fetch = set(list(referenced_sections_to_fetch)[:3])

    if not referenced_sections_to_fetch:
        return enriched

    print(f"🔗 NitiLink: พบการอ้างอิงถึงมาตรา {referenced_sections_to_fetch}")

    # ดึงมาตราที่ถูกอ้างอิงจาก ChromaDB โดยใช้ Metadata Filter
    for section_num in referenced_sections_to_fetch:
        try:
            results = collection.get(
                where={"section": section_num},
                include=["documents", "metadatas"]
            )
            if results and results["documents"]:
                for doc, meta in zip(results["documents"], results["metadatas"]):
                    enriched.append({
                        "document": doc,
                        "metadata": meta,
                        "source": "nitilink_cross_reference"
                    })
                    existing_sections.add(section_num)
                    print(f"   ↳ เพิ่มมาตรา {section_num} เข้าสู่บริบท")
        except Exception as e:
            print(f"   ⚠️ ไม่สามารถดึงมาตรา {section_num}: {e}")

    return enriched


# ==========================================
# 5. Thai Synonym Mapping (Query Expansion)
# ==========================================
# แปลงคำพูดภาษาปากเป็นคำศัพท์กฎหมายก่อนค้นหาใน Vector DB
THAI_LEGAL_SYNONYMS = {
    "ข้อมูลหลุด": "การละเมิดข้อมูลส่วนบุคคล",
    "ข้อมูลรั่ว": "การละเมิดข้อมูลส่วนบุคคล",
    "แอบเก็บ": "การเก็บรวบรวมข้อมูลส่วนบุคคลโดยมิชอบ",
    "ถอนสิทธิ์": "การถอนความยินยอม",
    "ลบข้อมูล": "สิทธิในการลบข้อมูล",
    "ขอดูข้อมูล": "สิทธิในการขอเข้าถึง",
    "โดนปรับ": "โทษปรับทางปกครอง",
    "ข้อมูลส่วนตัว": "ข้อมูลส่วนบุคคล",
    "ข้อมูลละเอียดอ่อน": "ข้อมูลส่วนบุคคลที่มีความอ่อนไหว",
    "ย้ายข้อมูล": "สิทธิในการโอนย้ายข้อมูล",
}

def expand_query(query: str) -> str:
    """ขยายคำถามภาษาปากเป็นคำศัพท์กฎหมาย (Zero-cost, ไม่ใช้โมเดล)"""
    expanded = query
    for slang, legal_term in THAI_LEGAL_SYNONYMS.items():
        if slang in query:
            expanded += f" ({legal_term})"
    return expanded


# ==========================================
# 6. Hybrid Search (Dense + Metadata)
# ==========================================
def search_legal_context(query: str, collection, top_k: int = TOP_K) -> list[dict]:
    """
    ค้นหาบริบททางกฎหมายจาก Vector DB
    ใช้ Dense Embedding Search (BGE-M3) + NitiLink Cross-referencing
    """
    # Query Expansion: แปลงคำภาษาปากเป็นคำศัพท์กฎหมาย
    expanded_query = expand_query(query)

    # Encode คำถามด้วย BGE-M3 (บน CPU)
    query_embedding = embedder.encode(expanded_query).tolist()

    # ค้นหา Top-K จาก ChromaDB
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )

    # แปลงผลลัพธ์เป็น List of Dicts
    primary_chunks = []
    for i in range(len(results["documents"][0])):
        primary_chunks.append({
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
            "source": "vector_search"
        })

    # เรียก NitiLink Engine เพื่อเสริมบริบท
    enriched_chunks = nitilink_enrich(primary_chunks, collection)

    return enriched_chunks


# ==========================================
# 6. Prompt Assembly
# ==========================================
def build_rag_prompt(query: str, context_chunks: list[dict]) -> str:
    """ประกอบ Prompt สำหรับ LLM โดยใส่บริบทกฎหมายที่ดึงมาได้"""

    # จัดกลุ่มบริบทตามแหล่งที่มา
    context_parts = []
    for chunk in context_chunks:
        meta = chunk.get("metadata", {})
        source_tag = "🔗 อ้างอิงไขว้" if chunk.get("source") == "nitilink_cross_reference" else "📄 ผลการค้นหา"
        section = meta.get("section", "ไม่ทราบ")
        chapter = meta.get("chapter", "ไม่ระบุ")
        text = chunk.get("document", chunk.get("text", ""))

        context_parts.append(
            f"[{source_tag}] หมวด {chapter} | มาตรา {section}\n{text}"
        )

    context_text = "\n\n---\n\n".join(context_parts)

    # ตัด Context ไม่ให้ยาวเกิน ~3000 ตัวอักษร เพื่อให้ Prompt รวมแล้วอยู่ใน 2048 tokens
    if len(context_text) > 3000:
        context_text = context_text[:3000] + "\n... (ตัดทอนเพื่อความกระชับ)"

    prompt = f"""คุณคือ "ผู้เชี่ยวชาญด้านกฎหมายคุ้มครองข้อมูลส่วนบุคคล (PDPA) ของไทย"

คุณจะได้รับบริบทจากบทบัญญัติกฎหมายจริง (Context) และคำถามจากผู้ใช้ (Question)
ให้ตอบคำถามโดยอ้างอิงจากบริบทที่ให้มาเท่านั้น ห้ามแต่งข้อกฎหมายขึ้นเองเด็ดขาด

คำตอบของคุณต้องแสดงกระบวนการคิดแบบ Legal Chain-of-Thought ตาม 4 ขั้นตอน:
1. บทบาท: ระบุบทบาทของบุคคล/นิติบุคคลที่เกี่ยวข้อง
2. ประเภทข้อมูล: จำแนกประเภทของข้อมูลส่วนบุคคล
3. การประเมินกฎหมาย: วิเคราะห์ฐานความชอบด้วยกฎหมาย อ้างอิงเลขมาตราที่เกี่ยวข้อง
4. คำแนะนำ: สรุปคำแนะนำในการปฏิบัติตามกฎหมายอย่างชัดเจน

=== บริบททางกฎหมาย (Context) ===
{context_text}

=== คำถาม (Question) ===
{query}

=== คำตอบ (Legal CoT Analysis) ==="""

    return prompt


# ==========================================
# 7. LLM Inference (Qwen + LoRA, 4-bit)
# ==========================================
_llm_model = None
_llm_tokenizer = None


def load_llm():
    """โหลด Qwen2.5-3B + LoRA Adapter ด้วย Unsloth 4-bit"""
    global _llm_model, _llm_tokenizer

    if _llm_model is not None:
        return _llm_model, _llm_tokenizer

    import unsloth
    from unsloth import FastLanguageModel

    print("🧠 Loading Qwen2.5-3B + PDPA LoRA Adapter (4-bit)...")
    _llm_model, _llm_tokenizer = FastLanguageModel.from_pretrained(
        model_name=LORA_PATH,  # โหลดจาก LoRA Adapter ที่เทรนไว้
        max_seq_length=2048,
        dtype=None,
        load_in_4bit=True,
    )
    FastLanguageModel.for_inference(_llm_model)  # เปิดโหมด Inference (เร็วกว่า Training)

    print("✅ LLM พร้อมใช้งาน")
    return _llm_model, _llm_tokenizer


def generate_answer(prompt: str, max_new_tokens: int = 512) -> str:
    """สร้างคำตอบจาก LLM"""
    model, tokenizer = load_llm()

    # สร้าง Chat Messages ตาม Qwen Format
    messages = [
        {"role": "system", "content": "คุณคือผู้เชี่ยวชาญด้านกฎหมาย PDPA ของไทย ให้ตอบเป็นภาษาไทยเท่านั้น"},
        {"role": "user", "content": prompt}
    ]

    # Tokenize
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)

    # Generate
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=0.3,      # ต่ำเพื่อให้คำตอบนิ่งและแม่นยำ
            top_p=0.9,
            repetition_penalty=1.15,
            do_sample=True,
        )

    # Decode เฉพาะส่วนคำตอบใหม่ (ตัด Prompt ออก)
    answer = tokenizer.decode(outputs[0][inputs["input_ids"].shape[-1]:], skip_special_tokens=True)

    # เคลียร์ VRAM Cache
    del inputs, outputs
    torch.cuda.empty_cache()
    gc.collect()

    return answer.strip()


# ==========================================
# 8. Full RAG Pipeline
# ==========================================
def ask_pdpa(query: str, collection, verbose: bool = True) -> str:
    """
    Full RAG Pipeline: Query → Search → NitiLink → Prompt → LLM → Answer
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"❓ คำถาม: {query}")
        print(f"{'='*60}")

    # Step 1: ค้นหาบริบท
    if verbose:
        print("\n🔍 กำลังค้นหาบริบททางกฎหมาย...")
    context_chunks = search_legal_context(query, collection)
    if verbose:
        print(f"   พบ {len(context_chunks)} Chunks (รวม NitiLink)")
        for c in context_chunks:
            src = "🔗" if c.get("source") == "nitilink_cross_reference" else "📄"
            sec = c.get("metadata", {}).get("section", "?")
            print(f"   {src} มาตรา {sec}")

    # Step 2: ประกอบ Prompt
    prompt = build_rag_prompt(query, context_chunks)

    # Step 3: สร้างคำตอบ
    if verbose:
        print("\n🧠 กำลังวิเคราะห์และสร้างคำตอบ...")
    answer = generate_answer(prompt)

    if verbose:
        print(f"\n{'─'*60}")
        print(f"📝 คำตอบ:\n{answer}")
        print(f"{'─'*60}")

    return answer


# ==========================================
# 9. Main: สร้าง Vector DB + ทดสอบถามคำถาม
# ==========================================
if __name__ == "__main__":
    import os, time as _time
    from datetime import datetime

    os.makedirs("logs", exist_ok=True)

    # สร้าง / โหลด Vector Store
    collection = build_vector_store(CHUNKS_PATH, CHROMA_DIR)

    # ทดสอบถามคำถาม 3 ข้อ
    test_questions = [
        "บริษัทเก็บข้อมูลลูกค้าโดยไม่ขอความยินยอม ทำได้ในกรณีใดบ้าง?",
        "ข้อมูลส่วนบุคคลที่มีความอ่อนไหว (Sensitive Data) มีอะไรบ้างตาม PDPA?",
        "หากบริษัทต่างชาติเก็บข้อมูลคนไทย ต้องปฏิบัติตาม PDPA หรือไม่?"
    ]

    rag_log_entries = []
    for q in test_questions:
        _q_start = _time.time()
        answer = ask_pdpa(q, collection)
        _q_duration = _time.time() - _q_start

        # ดึง context chunks เพื่อบันทึก
        ctx_chunks = search_legal_context(q, collection)
        retrieved_sections = [c.get("metadata", {}).get("section", "?") for c in ctx_chunks]

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "step": "step4_rag",
            "question": q,
            "retrieved_sections": retrieved_sections,
            "num_chunks": len(ctx_chunks),
            "answer_preview": answer[:500],
            "latency_seconds": round(_q_duration, 2),
        }
        rag_log_entries.append(log_entry)
        print("\n" + "=" * 80 + "\n")

    # บันทึกลงไฟล์ JSONL
    with open("logs/step4_rag_log.jsonl", "a", encoding="utf-8") as f:
        for entry in rag_log_entries:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    print(f"📝 บันทึกผล RAG {len(rag_log_entries)} คำถามลงไฟล์ logs/step4_rag_log.jsonl")

