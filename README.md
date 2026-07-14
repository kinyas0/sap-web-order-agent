# SAP Web Order Agent

Serbest metin veya yapılandırılmış girdiyle ürün/adet girilip onaylandığında doğrudan SAP'de satış siparişi açan bir Streamlit uygulaması. [SAP Mail Order Agent](https://github.com/kinyas0/sap-mail-order-agent) projesinin talep üzerine geliştirilen web tabanlı versiyonudur — mail yerine kullanıcı arayüzünden sipariş girişi sağlar.

## Nasıl çalışır

1. Kullanıcı arayüzden müşteriyi serbest metin (örn. "Eksen") veya doğrudan SAP kodu olarak girer.
2. Ürünleri serbest metin (ürün açıklaması) veya doğrudan SAP malzeme kodu olarak girer.
3. Serbest metin girişleri bir LLM (OpenRouter) ve/veya bulanık eşleştirme (fuzzy matching) ile SAP'deki `KNA1` / `MAKT` tablolarına karşı gerçek koda çevrilir.
4. Onaylandığında `BAPI_SALESORDER_CREATEFROMDAT2` ile SAP'de satış siparişi oluşturulur ve commit edilir, belge numarası ekranda gösterilir.

## Kurulum

```bash
pip install -r requirements.txt
```

`.env.example` dosyasını `.env` olarak kopyala ve kendi ortam bilgilerinle doldur:

```bash
cp .env.example .env
```

## Çalıştırma

```bash
streamlit run app.py
```

## Proje yapısı

```
config/
  settings.py       # .env tabanlı ayarlar
services/
  sap_service.py    # SAP RFC bağlantısı, müşteri/malzeme eşleştirme, sipariş oluşturma
  db_service.py      # (opsiyonel) transaction/agent-run/hata loglama
app.py               # Streamlit arayüzü ve akış
```

## Notlar

- SAP ve veritabanı kimlik bilgileri repoya dahil değildir, `.env` üzerinden verilir.

---

> Bu proje bir stajım sırasında canlı bir SAP ortamına karşı geliştirilip defalarca test edilmiştir. Bu repo, gerçek sunucu adresleri ve kimlik bilgileri temizlenmiş, genel kullanıma uygun hale getirilmiş halidir.
