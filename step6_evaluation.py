"""
Step 6: NitiBench Evaluation Framework
=======================================
Test Pipeline:
  [Test Dataset] → RAG Search → MultiMRR
                 → LLM Answer → Gemini Judge (Coverage + Contradiction)
                 → Guardrails → ThaiSafetyBench Block Rate
                 → RAGAS-style Faithfulness + Relevance

ทุกอย่างรันบน CPU/API — ไม่กระทบ VRAM 6GB
"""

import json
import re
import os
import time
import numpy as np
from typing import Optional

import google.generativeai as genai

# ==========================================
# 0. Configuration
# ==========================================
API_KEY = os.environ.get('GEMINI_API_KEY', 'AIzaSyBgKUgCaPi36I3mXRJhmsP4cFuV1fD1U_Q')
genai.configure(api_key=API_KEY)
judge_model = genai.GenerativeModel('gemini-2.0-flash')

# ==========================================
# 1. NitiBench Test Dataset
# ==========================================
NITIBENCH_DATASET = [
    {
        "question": "บริษัทเก็บข้อมูลลูกค้าโดยไม่ขอความยินยอมได้ในกรณีใดบ้าง",
        "expected_sections": ["๒๔"],
        "reference_answer": "มาตรา 24 กำหนดข้อยกเว้น 6 กรณี ได้แก่ เพื่อประโยชน์สาธารณะ ป้องกันอันตราย ปฏิบัติตามสัญญา ประโยชน์โดยชอบด้วยกฎหมาย ปฏิบัติตามกฎหมาย และจัดทำเอกสารประวัติศาสตร์"
    },
    {
        "question": "ข้อมูลส่วนบุคคลที่มีความอ่อนไหวมีอะไรบ้างตาม PDPA",
        "expected_sections": ["๒๖"],
        "reference_answer": "มาตรา 26 ระบุข้อมูลอ่อนไหว ได้แก่ เชื้อชาติ เผ่าพันธุ์ ความคิดทางการเมือง ศาสนา พฤติกรรมทางเพศ ประวัติอาชญากรรม ข้อมูลสุขภาพ ความพิการ สหภาพแรงงาน ข้อมูลพันธุกรรม ข้อมูลชีวภาพ"
    },
    {
        "question": "หากบริษัทต่างชาติเก็บข้อมูลคนไทย ต้องปฏิบัติตาม PDPA หรือไม่",
        "expected_sections": ["๕"],
        "reference_answer": "มาตรา 5 วรรค 2 กำหนดว่าผู้ควบคุมข้อมูลนอกราชอาณาจักรต้องปฏิบัติตาม PDPA หากเสนอสินค้า/บริการแก่คนในไทย หรือเฝ้าติดตามพฤติกรรมที่เกิดในไทย"
    },
    {
        "question": "สิทธิของเจ้าของข้อมูลส่วนบุคคลมีอะไรบ้าง",
        "expected_sections": ["๓๐", "๓๑", "๓๓", "๓๔", "๓๖"],
        "reference_answer": "เจ้าของข้อมูลมีสิทธิ ถอนความยินยอม เข้าถึงข้อมูล แก้ไขข้อมูล ลบข้อมูล ระงับการใช้ ส่งโอนข้อมูล และคัดค้านการประมวลผล"
    },
    {
        "question": "โทษของการละเมิด PDPA มีอะไรบ้าง",
        "expected_sections": ["๗๙", "๘๔", "๘๖", "๙๐"],
        "reference_answer": "โทษมีทั้งทางแพ่ง ทางอาญา (จำคุกไม่เกิน 1 ปี ปรับไม่เกิน 1 ล้านบาท) และโทษทางปกครอง (ปรับไม่เกิน 5 ล้านบาท)"
    },
]


# ==========================================
# 2. MultiMRR (Retrieval Evaluation)
# ==========================================
def compute_multi_mrr(retrieved_sections: list[str], expected_sections: list[str]) -> float:
    """
    Multi-Mean Reciprocal Rank: วัดว่าระบบดึงมาตราที่ถูกต้องมาได้ที่ตำแหน่งใด
    MRR = (1/|Q|) * Σ (1/rank_i) สำหรับแต่ละมาตราที่คาดหวัง
    ถ้าไม่พบ = 0
    """
    if not expected_sections:
        return 1.0

    reciprocal_ranks = []
    for expected in expected_sections:
        found = False
        for rank, retrieved in enumerate(retrieved_sections, 1):
            if retrieved == expected:
                reciprocal_ranks.append(1.0 / rank)
                found = True
                break
        if not found:
            reciprocal_ranks.append(0.0)

    return float(np.mean(reciprocal_ranks))


def evaluate_retrieval(search_fn, collection, dataset: list[dict]) -> dict:
    """ประเมิน Retrieval ทั้ง Dataset"""
    print("\n" + "=" * 60)
    print("📊 RETRIEVAL EVALUATION (MultiMRR)")
    print("=" * 60)

    mrr_scores = []
    recall_scores = []

    for item in dataset:
        chunks = search_fn(item["question"], collection)
        retrieved = [c.get("metadata", {}).get("section", "") for c in chunks]

        mrr = compute_multi_mrr(retrieved, item["expected_sections"])
        recall = len(set(retrieved) & set(item["expected_sections"])) / len(item["expected_sections"])

        mrr_scores.append(mrr)
        recall_scores.append(recall)

        print(f"  Q: {item['question'][:50]}...")
        print(f"    Expected: {item['expected_sections']} | Retrieved: {retrieved[:5]}")
        print(f"    MRR={mrr:.3f} | Recall={recall:.3f}")

    results = {
        "multi_mrr_mean": float(np.mean(mrr_scores)),
        "recall_mean": float(np.mean(recall_scores)),
        "per_question": mrr_scores,
    }
    print(f"\n  📈 Mean MultiMRR: {results['multi_mrr_mean']:.3f}")
    print(f"  📈 Mean Recall:   {results['recall_mean']:.3f}")
    return results


# ==========================================
# 3. LLM-as-a-Judge (Gemini API)
# ==========================================
JUDGE_PROMPT = """คุณคือผู้ตัดสินทางกฎหมายที่เป็นกลาง ให้ประเมินคำตอบของ AI ต่อไปนี้

=== คำถาม ===
{question}

=== คำตอบอ้างอิง (Ground Truth) ===
{reference}

=== คำตอบของ AI ที่ต้องประเมิน ===
{answer}

ให้ให้คะแนนใน 2 แกน (1-5):
1. **Semantic Coverage**: คำตอบครอบคลุมองค์ประกอบทางกฎหมายที่สำคัญครบถ้วนเพียงใด
2. **Contradiction**: คำตอบมีข้อมูลที่ขัดแย้งกับข้อเท็จจริงทางกฎหมายหรือไม่ (5=ไม่มีเลย, 1=ขัดแย้งรุนแรง)

ตอบเป็น JSON เท่านั้น:
{{"coverage": <1-5>, "contradiction": <1-5>, "reasoning": "<เหตุผลสั้นๆ>"}}"""


def judge_answer(question: str, reference: str, answer: str) -> dict:
    """ใช้ Gemini เป็นผู้ตัดสินคุณภาพคำตอบ"""
    prompt = JUDGE_PROMPT.format(question=question, reference=reference, answer=answer)

    try:
        config = genai.GenerationConfig(response_mime_type="application/json", temperature=0.0)
        response = judge_model.generate_content(prompt, generation_config=config)
        return json.loads(response.text)
    except Exception as e:
        print(f"    ⚠️ Judge Error: {str(e)[:80]}")
        return {"coverage": 0, "contradiction": 0, "reasoning": f"Error: {e}"}


def evaluate_generation(answers: list[str], dataset: list[dict]) -> dict:
    """ประเมินคุณภาพคำตอบทั้ง Dataset ด้วย LLM-as-a-Judge"""
    print("\n" + "=" * 60)
    print("⚖️  GENERATION QUALITY (LLM-as-a-Judge via Gemini)")
    print("=" * 60)

    coverage_scores = []
    contradiction_scores = []

    for i, (ans, item) in enumerate(zip(answers, dataset)):
        print(f"  [{i+1}/{len(dataset)}] Judging: {item['question'][:50]}...")
        result = judge_answer(item["question"], item["reference_answer"], ans)
        coverage_scores.append(result.get("coverage", 0))
        contradiction_scores.append(result.get("contradiction", 0))
        print(f"    Coverage={result.get('coverage',0)}/5 | Contradiction={result.get('contradiction',0)}/5")
        print(f"    Reasoning: {result.get('reasoning','N/A')[:100]}")
        time.sleep(6)  # Rate limit

    results = {
        "coverage_mean": float(np.mean(coverage_scores)),
        "contradiction_mean": float(np.mean(contradiction_scores)),
        "coverage_scores": coverage_scores,
        "contradiction_scores": contradiction_scores,
    }
    print(f"\n  📈 Mean Coverage:      {results['coverage_mean']:.2f}/5")
    print(f"  📈 Mean Contradiction: {results['contradiction_mean']:.2f}/5")
    return results


# ==========================================
# 4. ThaiSafetyBench (Red Teaming)
# ==========================================
def evaluate_safety(guardrails_pipeline, sample_size: int = 50) -> dict:
    """ทดสอบ Guardrails กับ ThaiSafetyBench"""
    print("\n" + "=" * 60)
    print("🔴 SAFETY EVALUATION (ThaiSafetyBench Red Teaming)")
    print("=" * 60)

    from datasets import load_dataset
    ds = load_dataset("typhoon-ai/ThaiSafetyBench", split="test", streaming=True)

    blocked = 0
    total = 0
    category_stats = {}

    for sample in ds:
        if total >= sample_size:
            break

        prompt = sample["prompt"]
        risk_area = sample.get("risk_area", "Unknown")

        result = guardrails_pipeline.run(prompt, verbose=False)

        if risk_area not in category_stats:
            category_stats[risk_area] = {"total": 0, "blocked": 0}
        category_stats[risk_area]["total"] += 1

        if result.blocked:
            blocked += 1
            category_stats[risk_area]["blocked"] += 1

        total += 1

    block_rate = blocked / total if total > 0 else 0

    print(f"\n  Tested: {total} prompts | Blocked: {blocked} | Rate: {block_rate:.1%}")
    print(f"\n  Per Category:")
    for cat, stats in sorted(category_stats.items()):
        rate = stats["blocked"] / stats["total"] if stats["total"] > 0 else 0
        print(f"    {cat[:50]}: {stats['blocked']}/{stats['total']} ({rate:.0%})")

    return {"block_rate": block_rate, "total": total, "blocked": blocked, "categories": category_stats}


# ==========================================
# 5. RAGAS-style Metrics (via Gemini API)
# ==========================================
FAITHFULNESS_PROMPT = """ประเมิน Faithfulness ของคำตอบ AI ต่อไปนี้
ทุกข้อกล่าวอ้างในคำตอบต้องอ้างอิงได้จาก Context ที่ให้

=== Context (เอกสารที่ดึงมา) ===
{context}

=== คำตอบของ AI ===
{answer}

ให้คะแนน Faithfulness (0.0-1.0):
- 1.0 = ทุกข้อกล่าวอ้างมีหลักฐานใน Context
- 0.0 = ทุกข้อกล่าวอ้างไม่มีหลักฐานรองรับ

ตอบเป็น JSON: {{"faithfulness": <0.0-1.0>, "unsupported_claims": ["<claim1>", ...]}}"""

RELEVANCE_PROMPT = """ประเมิน Answer Relevance ของคำตอบ AI ต่อไปนี้

=== คำถาม ===
{question}

=== คำตอบของ AI ===
{answer}

ให้คะแนน Relevance (0.0-1.0):
- 1.0 = ตอบตรงประเด็น กระชับ ไม่มีข้อมูลนอกเรื่อง
- 0.0 = ไม่ตอบคำถามเลย หรือสร้างข้อมูลโดยพลการ

ตอบเป็น JSON: {{"relevance": <0.0-1.0>, "reasoning": "<เหตุผล>"}}"""


def evaluate_ragas(answers: list[str], contexts: list[str], dataset: list[dict]) -> dict:
    """ประเมิน Faithfulness + Relevance แบบ RAGAS"""
    print("\n" + "=" * 60)
    print("📐 RAGAS EVALUATION (Faithfulness + Relevance)")
    print("=" * 60)

    faithfulness_scores = []
    relevance_scores = []
    config = genai.GenerationConfig(response_mime_type="application/json", temperature=0.0)

    for i, (ans, ctx, item) in enumerate(zip(answers, contexts, dataset)):
        print(f"  [{i+1}/{len(dataset)}] {item['question'][:50]}...")

        # Faithfulness
        try:
            resp = judge_model.generate_content(
                FAITHFULNESS_PROMPT.format(context=ctx[:2000], answer=ans[:1000]),
                generation_config=config
            )
            f_result = json.loads(resp.text)
            faithfulness_scores.append(f_result.get("faithfulness", 0))
        except Exception as e:
            print(f"    ⚠️ Faithfulness error: {e}")
            faithfulness_scores.append(0)

        time.sleep(6)

        # Relevance
        try:
            resp = judge_model.generate_content(
                RELEVANCE_PROMPT.format(question=item["question"], answer=ans[:1000]),
                generation_config=config
            )
            r_result = json.loads(resp.text)
            relevance_scores.append(r_result.get("relevance", 0))
        except Exception as e:
            print(f"    ⚠️ Relevance error: {e}")
            relevance_scores.append(0)

        time.sleep(6)

        print(f"    Faith={faithfulness_scores[-1]:.2f} | Relev={relevance_scores[-1]:.2f}")

    results = {
        "faithfulness_mean": float(np.mean(faithfulness_scores)),
        "relevance_mean": float(np.mean(relevance_scores)),
    }
    print(f"\n  📈 Mean Faithfulness: {results['faithfulness_mean']:.3f}")
    print(f"  📈 Mean Relevance:   {results['relevance_mean']:.3f}")
    return results


# ==========================================
# 6. Full Evaluation Pipeline
# ==========================================
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))

    # --- Phase 1: Retrieval Evaluation (CPU only) ---
    from step4_rag import build_vector_store, search_legal_context, CHUNKS_PATH, CHROMA_DIR

    collection = build_vector_store(CHUNKS_PATH, CHROMA_DIR)
    retrieval_results = evaluate_retrieval(search_legal_context, collection, NITIBENCH_DATASET)

    # --- Phase 2: Generation + RAGAS (ใช้ Mock answers เพื่อประหยัด VRAM) ---
    # ในการรันจริง ให้เปลี่ยนเป็น ask_pdpa() จาก step4_rag.py
    print("\n⚠️  ใช้ Reference Answers แทน LLM จริง เพื่อประหยัด VRAM")
    print("    (เปลี่ยนเป็น ask_pdpa() เมื่อต้องการทดสอบ LLM จริง)\n")

    mock_answers = [item["reference_answer"] for item in NITIBENCH_DATASET]
    mock_contexts = ["Context from RAG retrieval"] * len(NITIBENCH_DATASET)

    generation_results = evaluate_generation(mock_answers, NITIBENCH_DATASET)
    ragas_results = evaluate_ragas(mock_answers, mock_contexts, NITIBENCH_DATASET)

    # --- Phase 3: Safety (CPU only) ---
    from step5_guardrails import PDPAGuardrailsPipeline
    guardrails = PDPAGuardrailsPipeline()
    safety_results = evaluate_safety(guardrails, sample_size=30)

    # --- Final Report ---
    print("\n" + "=" * 60)
    print("📋 FINAL EVALUATION REPORT")
    print("=" * 60)
    print(f"  Retrieval MultiMRR:    {retrieval_results['multi_mrr_mean']:.3f}")
    print(f"  Retrieval Recall:      {retrieval_results['recall_mean']:.3f}")
    print(f"  Coverage (Judge):      {generation_results['coverage_mean']:.2f}/5")
    print(f"  Contradiction (Judge): {generation_results['contradiction_mean']:.2f}/5")
    print(f"  Faithfulness (RAGAS):  {ragas_results['faithfulness_mean']:.3f}")
    print(f"  Relevance (RAGAS):     {ragas_results['relevance_mean']:.3f}")
    print(f"  Safety Block Rate:     {safety_results['block_rate']:.1%}")
    print("=" * 60)

    # Save results
    all_results = {
        "retrieval": retrieval_results,
        "generation": generation_results,
        "ragas": ragas_results,
        "safety": safety_results,
    }
    with open("evaluation_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print("✅ ผลประเมินบันทึกลงไฟล์ evaluation_results.json")

    # ==========================================
    # บันทึกผลประเมินลง JSONL Log (พร้อม timestamp)
    # ==========================================
    os.makedirs("logs", exist_ok=True)
    from datetime import datetime
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "step": "step6_evaluation",
        "retrieval_mrr": retrieval_results.get("multi_mrr_mean", 0),
        "retrieval_recall": retrieval_results.get("recall_mean", 0),
        "coverage_mean": generation_results.get("coverage_mean", 0),
        "contradiction_mean": generation_results.get("contradiction_mean", 0),
        "faithfulness_mean": ragas_results.get("faithfulness_mean", 0),
        "relevance_mean": ragas_results.get("relevance_mean", 0),
        "safety_block_rate": safety_results.get("block_rate", 0),
        "safety_total": safety_results.get("total", 0),
        "safety_blocked": safety_results.get("blocked", 0),
        "safety_categories": safety_results.get("categories", {}),
    }
    with open("logs/step6_evaluation_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False, default=str) + "\n")
    print("📝 บันทึกผลประเมินลงไฟล์ logs/step6_evaluation_log.jsonl")

