# Serah terima pengembangan OSEE ERP

Repository ini berisi source aplikasi dan dokumentasi. Clone baru tidak berisi database perusahaan, PDF/Excel sumber, akun staf, password, API key, atau backup perusahaan. Dokumen arsitektur/audit memuat konteks bisnis internal; pertahankan repository sebagai **private**.

## Mulai bekerja

Pemilik repository harus terlebih dahulu memberikan akses GitHub kepada akun developer. Akses repository berbeda dari peran Owner/Finance/Marketing di aplikasi.

Pada Windows, siapkan Git dan Python 3.12, kemudian:

```powershell
git clone https://github.com/leonardusbayu/osee-erp.git
cd osee-erp
git switch -c work/nama-perubahan
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Perintah tersebut untuk **clone baru**. Jangan menimpa `.env` atau menghapus `.local` pada instalasi yang sudah berisi data. Buka `http://127.0.0.1:8000/login/` dan gunakan tombol demo untuk data sintetis, atau siapkan workspace perusahaan kosong dengan akun pribadi. Akun perusahaan di komputer pemilik tidak ikut tercipta di komputer developer.

Alternatif Windows: jalankan **Start OSEE.cmd** untuk launcher latar belakang dan **Stop OSEE.cmd** untuk menghentikannya. Launcher tidak melakukan auto-reload; restart setelah mengubah kode. Pada Linux/macOS, buat venv dengan `python3 -m venv .venv` dan gunakan `.venv/bin/python` pada perintah yang sama. Tidak diperlukan Codex untuk menjalankan aplikasi.

## Peta kode dan aturan penting

| Folder | Tanggung jawab |
| --- | --- |
| `core/` | Organisasi, tiga peran tim, audit, penomoran, MFA dan pemulihan akun. |
| `finance/` | Invoice, tagihan, ledger, rekonsiliasi, uang muka/refund dan penutupan. |
| `taxes/` | Review terdokumentasi, aturan hitung, bukti, kertas kerja tahunan dan chat. |
| `evidence/`, `imports/` | Lampiran privat dan arsip/pengecualian data sumber. |
| `director/`, `marketing/` | Perencanaan, budget, analisis dan pengamatan marketing. |
| `webapp/`, `templates/`, `static/` | Form, halaman, impor e-statement dan identitas visual OSEE. |
| `deploy/`, `scripts/`, `.github/workflows/` | Konfigurasi cloud, backup, transfer database dan CI. |

- Selalu batasi query dan mutasi pada organisasi serta membership aktif. Marketing tidak boleh memperoleh akses Finance melalui endpoint baru.
- Gunakan layanan domain untuk pembukuan; jangan melewati validasi dengan SQL, bulk update, atau flag transisi internal. Catatan yang telah diposting harus tetap dapat ditelusuri.
- Jaga transaksi atomik, penguncian organisasi dan idempotensi. Mutasi bank bukan otomatis pendapatan; invoice terbit dan layanan selesai memiliki pengakuan berbeda.
- Owner/Direktur mencatat review pajak profesional yang terdokumentasi. Peran Owner sendiri bukan bukti kompetensi pajak. AI tidak menetapkan pajak, mengubah ledger, atau mengirim pembayaran.
- Dokumen harus tetap privat dan diunduh melalui endpoint terotorisasi. Jangan menambahkan media publik atau menyimpan kunci di frontend.
- Data rekap 2026 bukan saldo kas, invoice, maupun omzet pajak yang telah direkonsiliasi. Gunakan data sintetis untuk pengembangan.

## Pemeriksaan sebelum pull request

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test --noinput
.\.venv\Scripts\python.exe -m unittest scripts.tests_backup
git diff --check
```

SQLite cukup untuk pengembangan lokal. Pengujian konkurensi memerlukan PostgreSQL; workflow GitHub menjalankan suite pada PostgreSQL, memeriksa konfigurasi produksi, membangun image, serta menguji pemulihan dan transfer database. Kredensial yang tertulis dalam workflow hanyalah kredensial database sintetis CI. Jangan menggantinya dengan rahasia produksi.

Pemeriksaan 9 September 2026 mencatat **346 tes PostgreSQL lulus**, 148 pemeriksaan halaman browser dan 10 alur finance. Itu hasil pada versi/tanggal tersebut; periksa hasil workflow untuk commit baru. Skrip browser lama dalam `tests/` memerlukan fixture/Playwright tersendiri; `tests/marketing-browser-smoke.cjs` masih merujuk fixture lokal yang tidak dikirim melalui Git. Jangan menjalankannya terhadap database perusahaan.

Kerjakan perubahan pada branch, sertakan migrasi dan tes yang relevan, lalu buka pull request dengan penjelasan perubahan dan hasil validasi. Menyalin source dari Git tidak menerapkan migrasi atau memperbarui server yang sedang berjalan.

## Prioritas dan batas yang belum selesai

1. **Data perusahaan:** tunggu e-statement Excel/CSV, rincian invoice/pembayaran dan dokumen pajak. Selesaikan pengecualian rekap serta rekonsiliasi saldo awal. Data/kunci dipindahkan melalui saluran privat yang disetujui pemilik, terpisah dari GitHub.
2. **Pajak:** review profil PT dan bukti secara profesional. Kertas kerja tahunan sudah tersedia; format resmi, pengiriman dan acceptance DJP belum menjadi integrasi yang tervalidasi.
3. **BNI:** API masih direview bank. Impor manual tersedia; jangan mengarang endpoint atau mengaktifkan klaim sinkronisasi langsung.
4. **Cloud:** tentukan server/domain, tinjau secrets dan akses, lakukan cutover PostgreSQL serta uji HTTPS dan pemulihan di luar host. Repository bukan layanan ERP yang sudah di-host.
5. **Alur lanjutan:** pendapatan kursus per sesi, refund setelah layanan, pembayaran pemasok sebelum tagihan, saldo awal subledger lengkap, payroll/SDM dan operations masih memerlukan pengembangan.
6. **AI/marketing:** kredensial, persetujuan provider, batas biaya dan evaluasi endpoint nyata diperlukan. Konektor platform marketing belum aktif.

Baca [hasil perbaikan](REMEDIATION-2026-09-09.md), [panduan Finance](FINANCE-OPERATING-GUIDE.md), [panduan cloud dan pemulihan](../deploy/README.md), serta [arsitektur ERP](FINANCE-ERP-ARCHITECTURE.md) sebelum mengubah area terkait. Audit lama dipertahankan sebagai riwayat, bukan status terbaru setiap fitur.
