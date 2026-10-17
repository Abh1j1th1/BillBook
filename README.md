
# BillBook

**Turn photos of GST invoices into clean, searchable, validated data.**

BillBook reads Indian GST purchase invoices from photos using a vision model (Gemma 4), checks the numbers the way an accountant would, and stores everything in a local database. You review anything suspicious, correct it in one click, and export a purchase register as CSV.

It runs fully offline with a local model through Ollama, or in the cloud through the Gemini API.

---

## Features

- **Batch scanning:** drop in one photo or fifty and read them all in a single run, with live progress.
- **Two reading modes:** *Local* (Ollama, private) or *Cloud* (Gemini API). Switch from the UI.
- **Structured extraction:** vendor and buyer, GSTINs, invoice number and date, taxable value, CGST/SGST/IGST, grand total, and every line item with HSN, quantity, rate and amount.
- **Built-in validation:** GSTIN format and checksum, tax logic, state-aware CGST/SGST versus IGST, line items versus taxable value, and total versus taxable plus tax. Each bill gets a status: 🟢 `ok`, 🟡 `check` or 🔴 `review`.
- **Duplicate protection:** the same vendor GSTIN and invoice number is never saved twice.
- **Browse and search:** filter by vendor, buyer, month, exact date or status, or search by invoice number or GSTIN.
- **Side-by-side review:** see the original photo, the line items and the validation issues together.
- **Human corrections:** edit the extracted JSON and save. Status is recomputed instantly.
- **Training data:** every correction is stored as a (model output, corrected output) pair and can be exported as JSONL for fine-tuning.
- **CSV export:** one row per line item, for a single bill or for everything matching your filters. Opens cleanly in Excel.
- **Light and dark mode** with a minimal interface.

---

## Quick start

### 1. Requirements

- Python 3.11 or newer
- **Either** [Ollama](https://ollama.com) with a vision-capable model, **or** a [Gemini API key](https://aistudio.google.com/apikey)

### 2. Install

```bash
git clone <your-repo-url>
cd BillBook

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

The pinned versions include Gradio 6.30, `ollama` (local mode) and `google-genai` (cloud mode).

### 3. Configure

Create a `.env` file in the project folder:

```env
# "ollama" (local, private) or "gemini" (cloud)
BILLBOOK_BACKEND=ollama

# Local mode
BILLBOOK_MODEL=gemma4:12b

# Cloud mode
GEMINI_API_KEY=your-key-here
```

For local mode, pull the model first (the name must match `ollama list`):

```bash
ollama pull gemma4:12b
```

### 4. Run

```bash
python app.py
```

Open the URL printed in the terminal (usually http://127.0.0.1:7860).

---

## Using the app

### 1. Scan bills

Choose a reading mode, upload your photos and press **Read and save bills**. In cloud mode you can paste an API key for the session, or leave it blank to use `GEMINI_API_KEY` from `.env`. The key is never written to disk. A results table shows what was saved, what was skipped as a duplicate, and what failed.

### 2. Browse and search

Filter or search your bills, then click a row to open it. You will see the original photo, the line items and the validation issues. If the model got something wrong, open **Edit extracted data (JSON)**, fix it and press **Save correction**. You can also export the single bill as CSV or delete it.

### 3. Export

- **Filtered bills to CSV:** exports whatever the Browse tab is currently showing.
- **Corrections as training data (JSONL):** exports every correction you have made as an image-output-correction triplet for fine-tuning.

---

## Fine-tuning with exported corrections

Every time you correct a bill in the **Browse** tab and hit **Save correction**, BillBook logs:
- The stored image path (`image`)
- The model's initial extraction attempt (`model_output`)
- Your verified correction (`corrected`)

Clicking **Export corrections as training data (JSONL)** in the **Export** tab outputs `training_data.jsonl`.

### 1. Structure of `training_data.jsonl`

```json
{
  "image": "stored_bills/20261017_184656_001.jpg",
  "model_output": { "vendor_name": "ABC Retail", "total": 1200.0 },
  "corrected": { "vendor_name": "ABC Retail Pvt Ltd", "total": 1250.0 }
}
```

### 2. Format conversion for Vision LLMs

Convert `training_data.jsonl` into standard conversational ShareGPT/ChatML format using this script (`prepare_dataset.py`):

```python
import json
import extractor

# Uses the exact extraction prompt from extractor.py
PROMPT = extractor.HEADER_PROMPT

dataset = []
with open("training_data.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        item = json.loads(line)
        dataset.append({
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": item["image"]},
                        {"type": "text", "text": PROMPT}
                    ]
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": json.dumps(item["corrected"], ensure_ascii=False)}
                    ]
                }
            ]
        })

with open("sft_dataset.json", "w", encoding="utf-8") as out:
    json.dump(dataset, out, indent=2)

print(f"Ready: {len(dataset)} examples exported to sft_dataset.json")
```

### 3. Fine-tuning with QLoRA (Unsloth / Hugging Face TRL)

Fine-tune vision models (e.g., Gemma 4 Vision or Qwen2.5-VL) on a single GPU with 4-bit LoRA:

```python
from unsloth import FastVisionModel
import torch
from trl import SFTTrainer, SFTConfig

# 1. Load 4-bit vision base model
model, tokenizer = FastVisionModel.from_pretrained(
    "google/gemma-4-vision-12b",
    load_in_4bit=True,
)

# 2. Add LoRA adapters
model = FastVisionModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    lora_alpha=16,
    lora_dropout=0,
)

# 3. Train
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    args=SFTConfig(
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        warmup_steps=5,
        max_steps=60,
        learning_rate=2e-4,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        output_dir="outputs",
    ),
)
trainer.train()

# 4. Save merged model or adapter
model.save_pretrained_merged("billbook_finetuned", tokenizer, save_method="merged_16bit")
```

### 4. Deploying your fine-tuned model back to BillBook

#### For Local Mode (Ollama):
1. Convert/quantize the weights to GGUF using `llama.cpp`.
2. Create a `Modelfile`:
   ```dockerfile
   FROM ./billbook_finetuned.gguf
   PARAMETER temperature 0.1
   ```
3. Register with Ollama:
   ```bash
   ollama create billbook-model -f Modelfile
   ```
4. Update your `.env`:
   ```env
   BILLBOOK_BACKEND=ollama
   BILLBOOK_MODEL=billbook-model
   ```

#### For Cloud Mode (Gemini / Vertex AI):
Upload the dataset to Google Cloud Vertex AI or Google AI Studio under **Supervised Tuning** to train a custom Gemini endpoint (e.g. `tunedModels/billbook-v1`), and set `BILLBOOK_GEMINI_MODEL=tunedModels/billbook-v1` in `.env`.

---

## How it works

```
 photo ──► prepare image ──► model: header & totals ──┐
                                                       ├─► validate ─► SQLite ─► UI / CSV
                             model: line items ───────┘
```

1. **Prepare:** the image is converted to JPEG and shrunk (1600 px for Ollama, 2000 px for Gemini) to keep requests fast.
2. **Extract:** two focused prompts run per bill, one for the header and totals and one for the line items. Output is constrained by Pydantic schemas, with automatic fallbacks (schema mode, then JSON mode, then plain text).
3. **Retry:** if the vendor name, buyer name or item table comes back empty, the model gets one more try with a more specific hint.
4. **Validate:** the extracted data is checked but never changed.
5. **Store:** the bill, its line items, a copy of the photo and the raw model output are saved.

### What gets validated

| Check                                                                     | Level   |
| ------------------------------------------------------------------------- | ------- |
| Vendor name missing, or identical to buyer name                           | error   |
| GSTIN bad format or checksum mismatch                                     | error   |
| IGST charged together with CGST/SGST                                      | error   |
| Line items do not add up to taxable value                                 | error   |
| Total does not match taxable value plus tax                               | error   |
| No line items extracted                                                   | error   |
| Buyer name, buyer GSTIN or vendor GSTIN missing                           | warning |
| Date cannot be parsed, or invoice number missing                          | warning |
| CGST and SGST differ                                                      | warning |
| State mismatch (same state with IGST, or different states with CGST/SGST) | warning |
| Line amount differs from quantity × rate                                 | warning |
| Total slightly above taxable plus tax (possible freight or cess)          | warning |
| Fields the model marked as hard to read                                   | warning |

Any error makes the status `review`. Warnings alone give `check`. No issues gives `ok`. The validator understands tax-inclusive line amounts, round-off, and dates written in many formats, including words such as "23rd July 2025".

---

## Configuration reference

All settings are optional environment variables, read from the environment or `.env`.

| Variable                  | Default                | Purpose                                        |
| ------------------------- | ---------------------- | ---------------------------------------------- |
| `BILLBOOK_BACKEND`      | `ollama`             | Default reading mode: `ollama` or `gemini` |
| `BILLBOOK_MODEL`        | `gemma4:12b`         | Ollama model name                              |
| `BILLBOOK_GEMINI_MODEL` | `gemma-4-26b-a4b-it` | Gemini model name                              |
| `BILLBOOK_NUM_CTX`      | `8192`               | Ollama context window                          |
| `BILLBOOK_MAX_SIDE`     | `1600` / `2000`    | Longest image side in pixels (local / cloud)   |
| `GEMINI_API_KEY`        | none                   | Gemini key (`GOOGLE_API_KEY` also works)     |

---

## Project structure

```
BillBook/
├── app.py          Gradio interface and wiring
├── extractor.py    Prompts, schemas and model backends (Ollama and Gemini)
├── validators.py   GSTIN checks, date parsing and invoice validation
├── db.py           SQLite storage, search and training-data export
├── export.py       CSV export
├── requirements.txt Pinned dependencies
├── LICENSE         Apache License 2.0
├── .env            Your settings (not committed)
├── billbook.db     Created on first run
└── stored_bills/   Copies of uploaded bill images (created on first run)
```

### Data model

SQLite, created automatically: `invoices` (header, totals, status, issues, raw model output), `line_items` (linked to an invoice) and `corrections` (original and corrected JSON for each human fix).

---

## Privacy

- **Local mode:** bills and data never leave your computer.
- **Cloud mode:** bill images are sent to the Gemini API for reading. The app shows a clear notice whenever this mode is active.
- All data is stored in `billbook.db` and `stored_bills/` on your machine. Back them up to keep your records.

---

## Tips and troubleshooting

- **Cloud mode says the key is missing or rejected:** paste a valid key in the app or set `GEMINI_API_KEY` in `.env`. The scan stops early because every remaining bill would fail the same way.
- **Local mode returns nothing:** check that Ollama is running and that `BILLBOOK_MODEL` matches a model shown by `ollama list`.
- **Rate limits (HTTP 429):** the app waits and retries automatically.
- **Many 🔴 results:** use clearer, well-lit, straight-on photos. A higher `BILLBOOK_MAX_SIDE` can help with small print.
- **Gradio errors after upgrading:** this app targets Gradio 6 (pinned in `requirements.txt`), where `theme` and `css` are passed to `launch()`.

---

## Limitations

- Built for Indian GST invoices. Other tax formats are not validated.
- Extraction quality depends on photo quality and the chosen model, so always review `check` and `review` bills.
- Validation flags likely mistakes. It does not replace professional accounting review.

---

## License

Licensed under the [Apache License 2.0](LICENSE).
