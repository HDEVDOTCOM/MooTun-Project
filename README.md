# หมูตุ๋น (MooToon)

LINE Chatbot สำหรับให้นักเรียนจดรายรับ รายจ่าย และเป้าหมายการออมผ่านข้อความภาษาไทย โปรเจกต์นี้เป็น MVP สำหรับทดลองกับเพื่อน นักเรียน และครู โดยข้อมูลของผู้ใช้แต่ละคนแยกจากกันด้วย LINE user ID

## ฟีเจอร์ที่ทำงานแล้ว

- จดรายจ่ายและรายรับ พร้อมยอด หมวด และวันที่
- เข้าใจ `วันนี้`, `เมื่อวาน`, วันที่ ค.ศ. และ พ.ศ.
- ดู 5 รายการล่าสุดและสรุปเดือนปัจจุบัน
- ลบรายการที่เพิ่งบันทึกล่าสุด
- ตั้งเป้าหมาย เพิ่มเงินออม และดูความคืบหน้า
- ปฏิเสธข้อความที่ขาดยอดหรือประเภทรายการ
- แยกข้อมูลตาม LINE user ID
- ป้องกัน webhook เดิมถูกบันทึกซ้ำ
- ใช้จำนวนเต็มหน่วยสตางค์เพื่อป้องกันความคลาดเคลื่อนของยอดเงิน
- จำกัดการทดลองไว้ที่แชตส่วนตัว

## ตัวอย่างคำสั่ง

```text
จ่าย 50 อาหาร
เมื่อวานซื้อหนังสือ 320
รับ 500 ค่าขนม
รายการล่าสุด
สรุปเดือนนี้
ลบล่าสุด
ตั้งเป้า 1500 ซื้อหนังสือ
ออม 100
เป้าหมาย
ช่วยเหลือ
```

## โครงสร้างไฟล์

```text
app.py                 FastAPI webhook และตัวควบคุมคำสั่ง
parser.py              แปลงข้อความภาษาไทยเป็นคำสั่ง
line_api.py            ตรวจลายเซ็นและตอบ LINE
database.py            ตั้งค่า SQLite/PostgreSQL
models.py              ตารางรายการเงิน เป้าหมาย และ webhook
repository.py          อ่านและเขียนข้อมูลโดยแยกผู้ใช้
messages.py            จัดรูปแบบข้อความภาษาไทย
tests/                 ชุดทดสอบ
docs/PRODUCT_SPEC.md    ขอบเขตผลิตภัณฑ์
docs/PILOT_TEST.md      แผนทดลองกับผู้ใช้
```

## รันในเครื่อง

ต้องมี Python 3.11 ขึ้นไป

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

บน Windows ใช้คำสั่งเปิด virtual environment และคัดลอกไฟล์ดังนี้:

```powershell
.venv\Scripts\activate
Copy-Item .env.example .env
```

แก้ไฟล์ `.env`:

```env
ENVIRONMENT=development
LINE_CHANNEL_SECRET=ค่าจาก LINE Developers Console
LINE_CHANNEL_ACCESS_TOKEN=ค่าจาก LINE Developers Console
DATABASE_URL=sqlite:///./mootoon.db
```

เริ่มเซิร์ฟเวอร์:

```bash
uvicorn app:app --reload --port 8000
```

เปิด <http://localhost:8000/> แล้วตรวจว่า `status` เป็น `ok` และเปิด
<http://localhost:8000/ready> เพื่อตรวจว่า LINE credentials กับฐานข้อมูลพร้อมใช้งาน

## รันทดสอบ

```bash
pytest -q
```

## Deploy บน Render

1. สร้าง GitHub repository และ push ไฟล์ทั้งหมด ยกเว้น `.env`
2. ใน Render เลือก **New > Blueprint** และเชื่อม repository
3. ตั้ง Environment Variables ตาม `.env.example`
4. สำหรับการทดลองหลายคน ให้ใช้ PostgreSQL/Supabase และตั้ง `DATABASE_URL` เป็น connection string ของฐานข้อมูล
5. เมื่อ deploy สำเร็จ ให้นำ URL ที่เติม `/webhook` ไปตั้งเป็น Webhook URL ใน LINE Developers Console
6. กด **Verify**, เปิด **Use webhook** และปิด Auto-response ใน LINE Official Account Manager

ตัวอย่าง:

```text
https://your-render-service.onrender.com/webhook
```

SQLite เหมาะกับการรันในเครื่อง ส่วนเซิร์ฟเวอร์ที่อาจ restart หรือ deploy ใหม่ควรใช้ PostgreSQL เพื่อไม่ให้ข้อมูลทดลองหาย
ระบบมี schema upgrade สำหรับสถานะการตอบ webhook ของ v0.1 อยู่ใน `init_db()` แล้ว

## การตั้งค่า LINE

- `LINE_CHANNEL_SECRET` อยู่ในแท็บ **Basic settings**
- `LINE_CHANNEL_ACCESS_TOKEN` ออกได้จากแท็บ **Messaging API**
- ห้าม commit `.env`, access token หรือ channel secret
- Webhook ตรวจลายเซ็นจาก raw request body ก่อนประมวลผลทุกครั้ง

## เอกสารทดลองและขอบเขต

- [Product specification](docs/PRODUCT_SPEC.md)
- [Pilot test plan](docs/PILOT_TEST.md)

ฟีเจอร์อ่านใบเสร็จ เสียง PDF, CSV, การแก้ไขรายการ และ LIFF dashboard วางไว้สำหรับระยะถัดไปหลัง MVP ผ่านการทดลอง Alpha
