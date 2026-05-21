"""
PDPA AI Assistant — Deep Debug Logging Module
===============================================
สถาปัตยกรรม:
  - ใช้ Python built-in `logging` + Custom JSONLHandler
  - บันทึกเป็น JSON Lines (.jsonl) → 1 บรรทัด = 1 Request Object
  - เขียนไฟล์แบบ Non-blocking ผ่าน QueueHandler + QueueListener (Threading)
  - ไม่ใช้ VRAM — ทุกอย่างทำงานบน CPU/Disk เท่านั้น

โครงสร้าง Log Object:
  {
    "timestamp": "2026-05-21T15:00:00",
    "raw_user_input": "...",
    "guardrail_input_status": {...},
    "rag_retrieved_context": [...],
    "llm_prompt_fed": "...",
    "llm_raw_response": "...",
    "guardrail_output_status": {...},
    "hardware_metrics": {...},
    "latency_ms": 1234
  }
"""

import json
import os
import time
import logging
import logging.handlers
from queue import Queue
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

# ==========================================
# 1. Structured Log Data Model
# ==========================================
@dataclass
class GuardrailInputLog:
    status: str = "N/A"           # PASSED / BLOCKED_PII / BLOCKED_JAILBREAK / BLOCKED_TOPIC
    pii_detected: list = field(default_factory=list)
    redacted_text: str = ""
    topic_guard_result: str = ""
    topic_confidence: float = 0.0

@dataclass
class RAGContextLog:
    section: str = ""
    similarity_score: float = 0.0
    source: str = "vector_search"  # vector_search / nitilink_cross_reference

@dataclass
class GuardrailOutputLog:
    status: str = "N/A"           # PASSED / BLOCKED_TOXICITY / BLOCKED_HALLUCINATION
    toxicity_detected: bool = False
    fact_check_passed: bool = True

@dataclass
class HardwareMetrics:
    vram_used_mb: float = 0.0
    vram_total_mb: float = 6144.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 16384.0

@dataclass
class RequestTrace:
    """1 Object = ข้อมูลครบถ้วนของ 1 คำถามจากผู้ใช้"""
    timestamp: str = ""
    raw_user_input: str = ""
    guardrail_input_status: dict = field(default_factory=dict)
    rag_retrieved_context: list = field(default_factory=list)
    llm_prompt_fed: str = ""
    llm_raw_response: str = ""
    guardrail_output_status: dict = field(default_factory=dict)
    hardware_metrics: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    error: Optional[str] = None


# ==========================================
# 2. JSONL File Handler (Non-blocking)
# ==========================================
class JSONLFileHandler(logging.FileHandler):
    """เขียน Log เป็น JSON Lines — 1 บรรทัดต่อ 1 JSON Object"""
    def emit(self, record):
        try:
            # record.msg เป็น dict จาก asdict(RequestTrace)
            if isinstance(record.msg, dict):
                log_data = record.msg
            else:
                log_data = {"message": str(record.msg)}
            line = json.dumps(log_data, ensure_ascii=False, default=str)
            with open(self.baseFilename, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            self.handleError(record)


# ==========================================
# 3. PDPALogger — Main Logger Class
# ==========================================
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "pdpa_trace.jsonl")

class PDPALogger:
    """
    ระบบบันทึกประวัติเชิงลึกสำหรับ PDPA AI Pipeline
    ใช้ QueueHandler เพื่อไม่ให้การเขียนไฟล์บล็อก UI Thread
    """
    def __init__(self, log_file: str = LOG_FILE):
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self.log_file = log_file

        # สร้าง Logger ที่แยกจาก Root Logger
        self.logger = logging.getLogger("pdpa_trace")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # ไม่ส่งต่อไป Root Logger

        # ลบ Handler เก่า (ป้องกันซ้ำเมื่อ Streamlit Rerun)
        self.logger.handlers.clear()

        # สร้าง Queue + QueueHandler (Non-blocking write)
        self._queue = Queue(-1)
        queue_handler = logging.handlers.QueueHandler(self._queue)
        self.logger.addHandler(queue_handler)

        # สร้าง JSONL Handler แล้วผูกกับ QueueListener (Background Thread)
        jsonl_handler = JSONLFileHandler(log_file, encoding="utf-8")
        self._listener = logging.handlers.QueueListener(
            self._queue, jsonl_handler, respect_handler_level=True
        )
        self._listener.start()

    def start_trace(self, raw_input: str) -> RequestTrace:
        """เริ่มบันทึก Trace ใหม่สำหรับ 1 คำถาม"""
        trace = RequestTrace(
            timestamp=datetime.now().isoformat(),
            raw_user_input=raw_input,
        )
        return trace

    def log_guardrail_input(self, trace: RequestTrace,
                            status: str, pii_list: list = None,
                            redacted_text: str = "",
                            topic_result: str = "", topic_conf: float = 0.0):
        """บันทึกผล Input Rails"""
        trace.guardrail_input_status = asdict(GuardrailInputLog(
            status=status,
            pii_detected=pii_list or [],
            redacted_text=redacted_text,
            topic_guard_result=topic_result,
            topic_confidence=topic_conf,
        ))

    def log_rag_context(self, trace: RequestTrace, sections: list):
        """บันทึกรายชื่อมาตราที่ดึงมาจาก RAG"""
        trace.rag_retrieved_context = [
            asdict(RAGContextLog(
                section=s.get("section", ""),
                similarity_score=s.get("score", 0.0),
                source=s.get("source", "vector_search"),
            )) for s in sections
        ]

    def log_llm(self, trace: RequestTrace, prompt: str, response: str):
        """บันทึก Prompt ที่ส่งเข้าโมเดลและคำตอบดิบ"""
        trace.llm_prompt_fed = prompt[:3000]    # จำกัดขนาดป้องกันไฟล์ใหญ่
        trace.llm_raw_response = response[:3000]

    def log_guardrail_output(self, trace: RequestTrace,
                             status: str, toxicity: bool = False,
                             fact_check: bool = True):
        """บันทึกผล Output Rails"""
        trace.guardrail_output_status = asdict(GuardrailOutputLog(
            status=status,
            toxicity_detected=toxicity,
            fact_check_passed=fact_check,
        ))

    def log_hardware(self, trace: RequestTrace):
        """จับข้อมูลการใช้ VRAM/RAM ณ เวลานั้น"""
        metrics = HardwareMetrics()
        try:
            import torch
            if torch.cuda.is_available():
                metrics.vram_used_mb = round(torch.cuda.memory_allocated() / 1024**2, 1)
                metrics.vram_total_mb = round(torch.cuda.get_device_properties(0).total_mem / 1024**2, 1)
        except Exception:
            pass
        try:
            import psutil
            mem = psutil.virtual_memory()
            metrics.ram_used_mb = round(mem.used / 1024**2, 1)
            metrics.ram_total_mb = round(mem.total / 1024**2, 1)
        except Exception:
            pass
        trace.hardware_metrics = asdict(metrics)

    def finalize(self, trace: RequestTrace, start_time: float):
        """คำนวณ Latency แล้วเขียนลงไฟล์ JSONL"""
        trace.latency_ms = round((time.time() - start_time) * 1000, 1)
        self.logger.info(asdict(trace))

    def shutdown(self):
        """ปิด Background Thread อย่างปลอดภัย"""
        self._listener.stop()


# ==========================================
# 4. Singleton Instance (ใช้ได้ทั้ง Pipeline)
# ==========================================
pdpa_logger = PDPALogger()
