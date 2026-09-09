# Arsitektur Modul Direktur OSEE

**PT Langkah Pintar Nusantara · One Stop English Education**  
**Tanggal:** 8 September 2026 · **Versi:** 1.0 · **Status:** arsitektur target; tahap awal modul sudah diimplementasikan.

Dokumen ini adalah kontrak rancangan modul Direktur. Acuan kondisi aplikasi: [status implementasi](IMPLEMENTATION-STATUS.md), [arsitektur Finance](FINANCE-ERP-ARCHITECTURE.md), [analisis sumber 2026](research/2026-recap-analysis.md), dan [identitas merek](BRAND-SOURCES.md). Implementasi awal 8 September 2026 meliputi dashboard sumber dan buku, kesiapan data, target, keputusan berjenjang, anggaran, rencana kas berbasis asumsi, simulasi, input/impor marketing, laporan PDF/CSV, dan chat Direktur dengan adapter OpenRouter opsional. Konektor langsung, kas terverifikasi, atribusi lengkap, evaluasi target otomatis, dan seluruh kontrol lanjutan dalam dokumen ini belum semuanya tersedia; gunakan status implementasi untuk membedakan fitur saat ini dan rancangan target.

**Panduan membaca:** mulai dari [menu direktur](#5-menu-dan-pengalaman-direktur-nonteknis), [kas](#8-kas-dan-proyeksi-13-minggu), [marketing](#9-marketing-dari-pengeluaran-menuju-hasil-bisnis), [budget](#10-budget-dan-kendali-belanja), [kebijakan](#11-target-arah-perusahaan-dan-kebijakan), dan [AI](#13-ai-direktur-penasihat-yang-menggunakan-bukti). Tim pengembang dapat melanjutkan ke [model data](#16-model-data-dan-granularitas), [API](#17-api-layanan-dan-event), [kriteria pengujian](#22-acceptance-criteria-yang-wajib-diuji), dan [roadmap](#25-roadmap-berdasarkan-manfaat-dan-dependensi).

## CAPABILITY — hasil yang ingin dicapai

Direktur dapat memahami pertumbuhan, keuntungan, kecukupan kas, efektivitas marketing, dan risiko OSEE; membandingkan pilihan tindakan; kemudian menetapkan target, anggaran, serta kebijakan yang memiliki penanggung jawab dan hasil terukur. AI membantu menjelaskan data dan menyiapkan usulan. Sistem menyimpan hubungan **data → analisis → pilihan → keputusan → pelaksanaan → hasil**, sehingga rapat direksi menghasilkan tindak lanjut yang dapat diperiksa.

Keberhasilan modul diukur dari keputusan yang lebih cepat dan dapat dipertanggungjawabkan: angka dapat ditelusuri, kas untuk layanan yang sudah dijanjikan terlindungi, anggaran tidak dihitung ganda, dan dampak kebijakan dipantau. Penghematan waktu dan perbaikan hasil bisnis harus diukur setelah penggunaan; bukan janji persentase tertentu.

## CONSTRAINTS — fakta, batasan, dan keputusan arsitektur

### 1. Konteks bisnis yang menjadi dasar

| Hal | Fakta yang sudah dikonfirmasi | Implikasi rancangan |
|---|---|---|
| Badan usaha | PT Langkah Pintar Nusantara, PT non-perorangan; saat ini non-PKP | Profil badan dan kebijakan pajak dibaca dari Finance/Tax, dengan tanggal berlaku dan bukti. Direktur tidak mengubah perlakuan pajak melalui chat. |
| Skala omzet | Pengguna menyatakan di bawah Rp4,8 miliar | Bukan bukti kelengkapan omzet setahun atau hak otomatis atas tarif pajak tertentu. |
| Produk | TOEFL ITP resmi, TOEFL iBT resmi, kursus bahasa Inggris | Unit, biaya, siklus penjualan, dan pengakuan pendapatan harus dipisahkan per produk. |
| Mitra | Lebih dari 40 reseller; membeli dari OSEE lalu menentukan harga jual sendiri | Pendapatan OSEE berasal dari transaksinya dengan mitra. Penjualan lanjutan mitra tidak ditambahkan menjadi omzet OSEE. |
| Pembayaran mitra | Per pesanan sebelum tes | Peserta, pesanan, uang diterima, dan tes selesai merupakan kejadian berbeda. Uang muka belum menjadi bukti laba atau uang bebas untuk iklan. |
| Harga ITP | Harga IIEF berubah; indikasi sekitar Rp450.000 setelah pajak, harga mitra sekitar Rp500.000–Rp530.000 | Angka indikatif bukan biaya historis setiap peserta. Simpan versi harga, tanggal berlaku, kontrak, biaya aktual, dan biaya estimasi secara terpisah. |
| BNI | API direncanakan diperoleh; akses dan cakupan belum diverifikasi | Rancang impor file dan konektor baca-saja. Jangan menampilkan status tersambung sebelum uji akses dan rekonsiliasi. |
| AI | OpenRouter dipilih pengguna | Buat jalur AI Direktur baru dengan kontrol konteks privat; chat pajak yang ada belum mendukung analisis data perusahaan. |
| Marketing | Kanal dikonfirmasi: mitra, Meta Ads, Google Ads, WhatsApp, SEO, dan sales. Akun, izin API, CRM, struktur tim, serta nominal budget belum diketahui | Semua kanal masuk model awal. Impor terstruktur dapat dimulai sebelum konektor akun tersedia. |

**Aturan tetap:** angka sumber tidak diubah diam-diam; transaksi yang dibukukan tetap milik Finance; semua akses dibatasi perusahaan dan izin; AI tidak membayar, mengajukan pajak, atau mengaktifkan kampanye; keputusan memiliki pembuat, penyetuju, versi, dan tanggal berlaku.

**Pilihan arsitektur yang direkomendasikan:** memperluas Django menjadi aplikasi modular dalam satu deployment, memakai PostgreSQL untuk produksi, layanan perhitungan deterministik, tabel agregat analitik, dan worker latar belakang. Pemisahan menjadi microservices, data warehouse terpisah, serta vector database belum diperlukan untuk tahap awal.

**Kebijakan yang masih harus ditetapkan perusahaan:** batas persetujuan rupiah, cadangan kas minimum, horizon kewajiban terlindungi, definisi mitra aktif, target pertumbuhan, alokasi biaya bersama, model atribusi, serta izin pengiriman konteks agregat ke penyedia AI.

### 2. Apa yang dapat dibaca dari data 2026 sekarang

| Sumber/kebutuhan | Kondisi berdasarkan impor yang telah dilakukan | Perlakuan di Direktur |
|---|---|---|
| Rekap ITP Januari–September | Total cetak 6.520 kemunculan peserta dan Rp3.564.480.000; September parsial | Label **Nilai rekap sumber**, bukan pendapatan terverifikasi atau penerimaan bank. |
| Detail dan pengecualian | Jumlah detail 6.521; 14 pengecualian perlu ditinjau | Tampilkan selisih dan lokasi sumber. Resolusi membuat catatan baru, bukan menimpa PDF. |
| Data mitra | 44 label sumber berbeda | Belum 44 identitas badan/mitra terverifikasi; penggabungan alias perlu persetujuan. |
| Cakupan waktu | September hanya memiliki baris 2 September terisi; Oktober–Desember tidak tersedia | Bulan parsial dan tidak tersedia tidak menjadi nol. Tidak menghitung pertumbuhan tahunan tanpa pembanding sebanding. |
| Invoice IIEF | Satu invoice 50 peserta; total Rp22.200.000; tersimpan sebagai tagihan draf | Boleh diperiksa sebagai bukti tagihan tersebut; bukan biaya rata-rata seluruh tahun atau bukti pembayaran. |
| Buku, bank, invoice pelanggan | Impor rekap tidak membuat jurnal, mutasi bank, invoice pelanggan, atau harga aktif | Kartu saldo kas, laba, piutang, dan omzet terverifikasi menunggu data yang sesuai. Periksa ulang kondisi saat implementasi. |
| Marketing | Enam kanal dikonfirmasi digunakan, tetapi belum ada dataset biaya, kampanye, lead, dan atribusi yang terverifikasi | CAC/ROAS tampil **Belum dapat dihitung**, dengan kebutuhan data yang konkret. |

Data yang datang setelah impor wajib melalui pemeriksaan cakupan dan rekonsiliasi yang sama. Jangan mengunci UI selamanya pada kondisi 7 September; gunakan pemeriksaan kesiapan aktual per metrik.

## IMPLEMENTATION CONTRACT — pengalaman dan rancangan teknis

### 3. Posisi modul dalam ERP

```mermaid
flowchart TB
    F[Finance dan Tax] --> A[Analytics: definisi metrik dan snapshot]
    I[Arsip rekap dan pengecualian impor] --> A
    M[Marketing: kampanye, lead, atribusi] --> A
    O[Operasional Tes dan Kursus: kapasitas, penyelesaian] --> A
    P[Mitra: kontrak, pesanan, hubungan] --> A
    A --> D[Direktur: ringkasan, pertumbuhan, risiko]
    A --> S[Planning: anggaran dan simulasi]
    D --> AI[AI Direktur melalui gateway OpenRouter]
    S --> AI
    AI --> U[Usulan keputusan dengan bukti]
    U --> H[Peninjauan dan persetujuan manusia]
    H --> K[Kebijakan dan tugas berversi]
    K --> X[Pelaksanaan oleh modul dan staf berwenang]
    X --> A
```

Panah menunjukkan kontrak integrasi yang dirancang, termasuk modul yang belum dibangun. Semua proses tetap dapat berjalan tanpa AI. Chat bukan jalan khusus untuk melewati persetujuan.

| Domain/paket | Tanggung jawab | Batas kepemilikan |
|---|---|---|
| `core` — tersedia, perlu perluasan | Perusahaan, identitas, izin, audit, event outbox | Menambah kapabilitas Direktur/Marketing dan mekanisme pengiriman event; peran owner lama tidak otomatis menjadi seluruh izin baru. |
| `finance`, `taxes` — tersedia sebagian | Buku, bank, invoice, tagihan, harga, status pajak | Satu sumber transaksi keuangan. Direktur membaca; koreksi tetap melalui layanan Finance/Tax. |
| `imports`, `evidence` — tersedia | Arsip privat, provenance, rekap, pengecualian | Sumber historis tidak otomatis menjadi transaksi. |
| `marketing` — baru | Kanal, kampanye, lead, touchpoint, biaya platform, hubungan konversi | Tidak membuat pendapatan hanya karena platform melaporkan conversion. |
| `analytics` — baru | Definisi metrik, mapping dimensi, agregasi, kualitas, snapshot, rekonsiliasi | Tidak menjadi buku kedua dan tidak menerima SQL bebas dari AI. |
| `director` — baru | Ringkasan, sasaran, keputusan, kebijakan, tindak lanjut | Memiliki persetujuan bisnis; bukan hak superuser teknis. |
| `director.planning` — subpaket awal | Budget, komitmen, forecast, skenario, eksperimen | Dapat dipisahkan kelak jika batas domain membesar; bukan microservice terpisah sejak awal. |
| `director.ai` — subpaket awal | Orkestrasi percakapan, pemilihan fakta, tools, validasi, biaya | Provider transport dapat dibagi dengan chat pajak setelah refactor; izin, prompt, konteks, dan audit tetap terpisah. |
| Modul masa depan | Operasional Tes, Akademik, Mitra, SDM | Menerbitkan kontrak data dan event; tidak menulis tabel Direktur atau buku secara langsung. |

Awal implementasi mempertahankan template Django, sesi, CSRF, format rupiah, logo, dan token CSS bersama. API digunakan untuk grafik, simulasi, chat, dan integrasi; tidak perlu memindahkan seluruh aplikasi ke SPA.

### 4. Aktor dan pemisahan kewenangan

| Aktor | Dapat membaca | Dapat mengusulkan/mengerjakan | Dapat menyetujui |
|---|---|---|---|
| Direktur | Ringkasan perusahaan, forecast, laba, budget, mitra, risiko | Sasaran, skenario, arahan, draf kebijakan | Budget/kebijakan sesuai mandat, dengan peninjauan keuangan yang diwajibkan |
| Finance | Keuangan dan bagian budget yang relevan | Rekonsiliasi, biaya, komitmen, asumsi kas, pemeriksaan usulan | Kesiapan data dan pemeriksaan keuangan; tidak otomatis final budget |
| Marketing lead | Kampanye, funnel, budget tim, metrik kontribusi yang diizinkan | Kampanye, eksperimen, usulan budget, update hasil | Belanja dalam delegasi yang disetujui, jika kebijakan tersebut sudah aktif |
| Operations lead | Kapasitas, jadwal, backlog layanan | Kelayakan kapasitas dan biaya pelaksanaan | Kesiapan operasional, bukan pajak atau pembayaran |
| Peninjau pajak berwenang | Data dan usulan perlakuan pajak sesuai akses | Review dasar hukum dan perlakuan | Keputusan pajak dalam workflow Tax |
| Auditor/pembaca rapat | Snapshot dan keputusan sesuai mandat | Catatan review jika diizinkan | Tidak ada mutasi keuangan/budget |
| Administrator akun | Pengguna, konfigurasi teknis sesuai mandat | Pemulihan akses dan konektor | Tidak otomatis menyetujui keputusan bisnis |
| AI/service account | Fakta agregat yang diloloskan kebijakan | Analisis dan proposal terstruktur | Tidak memiliki izin persetujuan atau eksekusi eksternal |

Implementasi memakai kapabilitas seperti `director.view`, `decision.propose`, `decision.approve`, `budget.review`, `budget.approve`, `marketing.manage`, `ai.private_aggregate.use`, `report.export`. Membership tetap berlingkup perusahaan. Pengguna dapat memiliki beberapa penugasan kapabilitas; migrasi dari satu field role harus mempertahankan akses lama secara eksplisit. Jangan mengandalkan pemeriksaan tampilan menu sebagai otorisasi.

Untuk tim kecil dengan satu orang menjalankan dua fungsi, tampilkan konflik peran. Pengecualian persetujuan harus merupakan kebijakan terdokumentasi dengan alasan dan jejak audit; sistem tidak berpura-pura memiliki dua pemeriksa independen.

### 5. Menu dan pengalaman direktur nonteknis

| Halaman | Pertanyaan yang dijawab | Isi dan tindakan utama |
|---|---|---|
| **Ringkasan Direktur** | Apa yang perlu saya perhatikan hari ini? | Maksimal 6 kartu utama, grafik tren, 3–5 perhatian prioritas, keputusan menunggu persetujuan, tombol Tanya AI |
| **Pertumbuhan & Produk** | Produk/kanal mana yang tumbuh secara sehat? | Volume, nilai transaksi, pendapatan, kontribusi, mix produk, pembanding sebanding |
| **Kas & Kewajiban** | Apakah uang cukup sampai 13 minggu ke depan? | Saldo bertanggal, proyeksi mingguan, kewajiban layanan, minimum kas, rincian kekurangan |
| **Marketing & Anggaran** | Pengeluaran mana yang menghasilkan bisnis? | Funnel, biaya, hasil teratribusi, budget, komitmen, pembayaran, hasil eksperimen |
| **Mitra & Pelanggan** | Seberapa sehat dan terkonsentrasi jaringan penjualan? | Mitra aktif/berulang, volume, kontribusi, konsentrasi, keterlambatan, alias belum disahkan |
| **Target & Kebijakan** | Arah perusahaan apa yang sedang dijalankan? | Sasaran tahunan/kuartalan, keputusan, pemilik tugas, tenggat, versi kebijakan |
| **Simulasi** | Apa dampak perubahan harga, biaya, volume, dan budget? | Baseline, konservatif, rencana, optimistis; biaya/kas/kapasitas; asumsi yang dapat diubah |
| **AI Direktur** | Jelaskan masalah dan bantu menilai pilihan | Percakapan privat, sumber angka, skenario, draf usulan; tidak ada tombol eksekusi tersembunyi |
| **Laporan Rapat** | Apa dasar keputusan dan progres bulan ini? | Paket PDF/CSV bersnapshot, narasi terverifikasi, status data, daftar keputusan dan hasil |
| **Kesiapan Data** | Angka mana belum bisa dipercaya atau dihitung? | Sumber terlambat, cakupan, rekonsiliasi, pengecualian, pemilik dan langkah penyelesaian |

Urutan Ringkasan: periode dan status data → kas/kewajiban → pertumbuhan/kontribusi → kebutuhan keputusan. Kartu finansial yang tidak siap menampilkan “Belum dapat dihitung”, bukan Rp0. Klik kartu membuka rumus singkat, cakupan, sumber, dan transaksi sesuai izin.

Pola setiap kartu: **angka + satuan → dibanding apa → data sampai kapan → status bukti → penjelasan sederhana → tindakan**. Contoh pada data awal: “Nilai rekap ITP Rp3,56 miliar · Januari–September parsial · dari PDF · 14 catatan perlu ditinjau.” Jangan mengganti label itu dengan “Pendapatan 2026”.

UI memakai Bahasa Indonesia, angka `Rp12.500.000`, tanggal lokal, dan istilah dengan penjelasan: “Biaya mendapatkan pembeli baru (CAC)”. Tombol utama: **Lihat sumber**, **Bandingkan pilihan**, **Buat usulan**, **Tinjau**, **Setujui versi ini**, **Tugaskan**. Konfirmasi persetujuan menampilkan perubahan konkret, bukan sekadar “Yakin?”.

Gunakan logo resmi `static/brand/osee-logo.png` di bidang terang dan token bersama: merah tindakan `#B51010`, merah gelap `#730808`, teks `#222121`, latar `#F8F8F8`. Merah merek bukan penanda semua metrik turun; status juga memakai ikon dan teks. Grafik harus menyediakan tabel alternatif, tidak bergantung pada warna, dapat dipakai dengan keyboard, dan tetap terbaca di ponsel. Detail teknis tidak muncul di alur utama.

### 6. Kamus metrik dan standar bukti

Setiap definisi metrik memiliki `key`, `version`, label Indonesia, unit, rumus terdaftar dalam kode, basis waktu, dimensi yang boleh dipakai, sumber, syarat kelengkapan, aturan koreksi, pemilik, dan tanggal berlaku. Rumus tidak dapat diedit bebas menjadi SQL melalui UI atau chat.

Setiap hasil memiliki nilai nullable, `as_of`, periode tercakup, snapshot ID, versi rumus, sumber dan status verifikasi, completeness, freshness, serta alasan jika tidak tersedia. Status memiliki beberapa sumbu, bukan satu “skor kepercayaan” yang menutupi masalah:

- **Asal/basis:** rekap dilaporkan, transaksi dibukukan, bank, platform, atau skenario.
- **Verifikasi:** belum diperiksa, telah diperiksa, direkonsiliasi, atau periode ditutup sesuai jenis sumber.
- **Cakupan:** lengkap, parsial, tidak tersedia; beserta pembilang/penyebut yang diketahui.
- **Kesegaran:** waktu sumber dan waktu sinkronisasi, bukan hanya waktu halaman dibuka.
- **Ketidakpastian:** asumsi, rentang, dan tingkat kematangan cohort; bukan persentase keyakinan buatan LLM.

| Metrik | Definisi implementasi | Syarat dan batas |
|---|---|---|
| Nilai rekap sumber | Jumlah nilai cetak dalam snapshot sumber terpilih | Basis rekap; tidak dicampur dengan ledger. |
| Nilai pesanan | Nilai order yang diterima pada tanggal order, setelah diskon dalam order | Booking bukan kas atau pendapatan; order batal/kredit dilacak terpisah. |
| Pendapatan diakui | Neto posting pendapatan dan koreksi terkait untuk periode layanan/pembukuan yang disahkan Finance | Bukan total invoice atau total bank masuk; label parsial jika pengakuan kursus belum lengkap. |
| Kas pelanggan diterima | Arus masuk bank yang dipetakan ke pelanggan, dikurangi pengembalian pada basis kas | Transfer antar rekening sendiri dan modal/pinjaman bukan penjualan. Unmatched terlihat terpisah. |
| Peserta tes | Jumlah unit tes sesuai status: dipesan, dibayar, dijadwalkan, selesai | Satu orang dapat mengikuti beberapa tes. Jumlah peserta bukan pembeli unik. |
| Pendapatan per unit | Pendapatan teralokasi / unit layanan selesai dengan basis produk/periode sama | Jangan membagi pendapatan semua produk dengan peserta ITP saja. Penyebut nol → tidak tersedia. |
| Laba kotor | Pendapatan diakui − biaya langsung yang dipasangkan pada layanan tersebut | Menunggu alokasi biaya dan periode; tagihan pemasok bulan lain perlu pengaitan layanan. |
| Kontribusi sebelum akuisisi | Pendapatan neto − biaya layanan variabel − biaya transaksi dan fee refund tambahan | Pokok refund/kredit sudah mengurangi pendapatan neto sekali; tidak dipotong lagi sebagai biaya. Kebijakan biaya eksplisit; bukan laba bersih. |
| Kontribusi setelah akuisisi | Kontribusi sebelum akuisisi − biaya akuisisi yang dialokasikan dengan basis konsisten | Total alokasi tidak melebihi biaya sumber; mencegah iklan dipotong lagi di tingkat perusahaan. |
| Laba operasi | Pendapatan − biaya layanan − beban operasi dalam periode | Tampil lengkap hanya jika cakupan biaya, penggajian, dan alokasi memadai. |
| Pertumbuhan | `(nilai saat ini − nilai pembanding) / nilai pembanding` | Basis, cakupan, durasi, dan dimensi sama. Basis nol → “mulai dari nol”; basis negatif → delta rupiah, bukan growth standar. |
| Mitra aktif | Mitra canonical dengan kejadian bisnis yang ditetapkan dalam jendela waktu | Usulan awal: memiliki order dibayar dalam 90 hari; harus disetujui, bukan definisi sumber lama. |
| Repeat mitra | Mitra cohort yang memesan lagi dalam jendela / mitra cohort yang sudah cukup lama diamati | Mitra baru yang belum matang bukan otomatis tidak kembali. |
| Konsentrasi mitra | Porsi pendapatan atau kontribusi top-1/top-5 terhadap total basis sama | Tampilkan basis dan risiko kontrak; hasil sementara jika identitas belum terpetakan. |
| Utilisasi tes/kelas | Unit terisi atau dilayani / kapasitas yang benar-benar tersedia untuk jadwal tersebut | Memerlukan data Operations; tidak dibuat dari jumlah peserta saja. |
| Refund/cancel rate | Order atau nilai refund/cancel / basis cohort terkait | Pisahkan pembatalan, tidak hadir, penjadwalan ulang, dan refund uang. |
| Target tercapai | Aktual comparable / target untuk basis dan periode yang sama | Target versi awal dan revisi ditampilkan; revisi tidak menghapus kegagalan target lama. |

Growth bulanan penuh tidak menggunakan September parsial dibanding Agustus penuh. Mode MTD hanya tersedia jika tanggal dan cakupan hari cukup pasti; konflik tahun Januari dan tanggal gabungan tetap menghalangi analisis harian yang bergantung padanya. YoY memerlukan data tahun sebelumnya yang sebanding. Penurunan peserta sendiri tidak membuktikan penyebabnya marketing.

### 7. Dimensi analisis dan kualitas keuntungan

Dimensi awal: perusahaan, tanggal pesanan, tanggal pembayaran, tanggal layanan, tanggal posting, produk ITP/iBT/kursus, format online/offline, direct/reseller/institusi bila terverifikasi, mitra canonical, kelompok harga, kampanye, cohort pembeli/mitra. Cabang, lokasi, tim, dan pengajar ditambahkan jika ada master data nyata.

**ITP:** harga jual disimpan per order dari perjanjian yang berlaku. Biaya pemasok dipasangkan melalui baris tagihan dan batch tes; biaya masih berupa quote ditandai estimasi. PPN pemasok dan perlakuan biaya memakai hasil review Finance, bukan asumsi universal di Analytics. Biaya nonkreditabel yang sudah termasuk total tagihan tidak ditambahkan dua kali.

**iBT:** gunakan SKU, pemasok/kontrak, biaya, jadwal pembayaran, refund, dan kapasitas sendiri. Jangan mewarisi Rp450.000 atau margin ITP. Tonggak layanan mungkin berbeda menurut kontrak; jangan mengasumsikan penyerahan voucher, redemption, atau selesai tes sebagai tonggak pengakuan tanpa bukti dan keputusan Finance.

**Kursus:** pisahkan murid, enrolment, paket, sesi, kelas, jam pengajar, pembayaran, dan layanan yang selesai. Pendapatan bertahap bergantung pada penyelesaian fitur Finance/Academics. Biaya pengajar tetap, per sesi, atau per murid harus dimodelkan sesuai kontrak; tanpa itu jangan mengklaim profit per kelas.

Analisis pertumbuhan memecah perubahan menjadi volume, harga, mix produk/mitra, diskon/refund, biaya pemasok, dan biaya marketing. Gunakan dekomposisi berurutan dengan urutan berversi atau metode simetris yang disepakati; angka jembatan harus tepat merekonsiliasi perubahan total. “Mix effect” bukan alasan mengubah harga historis.

Tampilan margin memisahkan **terverifikasi**, **estimasi dengan biaya belum lengkap**, dan **tidak dapat dihitung**. Cakupan biaya dihitung berdasarkan unit/nilai yang dapat dipetakan; “80% lengkap” hanya boleh tampil jika penyebutnya diketahui. Biaya tak teralokasi tetap di bucket tersendiri, bukan hilang.

### 8. Kas dan proyeksi 13 minggu

Tujuannya memperlihatkan kapan dana berpotensi tidak cukup, siapa pemilik kewajiban, dan perubahan rencana apa yang bisa diuji. Proyeksi mingguan adalah alat manajemen; bukan laporan arus kas statutory yang sudah lengkap.

**Empat angka yang berbeda:**

1. **Saldo bank tercatat**, dari statement/API bertanggal, dengan cakupan rekening dan perbedaan rekonsiliasi.
2. **Saldo buku**, dari ledger dan saldo awal yang disahkan; selisih bank-buku ditampilkan.
3. **Kewajiban kas dan layanan**, termasuk IIEF untuk tes sudah dijual, pengajar, refund, payroll, sewa, pajak terverifikasi, serta komitmen lain yang belum dibayar.
4. **Ruang kas untuk keputusan**, hasil proyeksi setelah kewajiban dan batas cadangan manajemen; bukan seluruh saldo bank.

```text
Kas akhir minggu t = kas awal t
                  + penerimaan yang diperkirakan terjadi pada minggu t
                  - pembayaran yang diperkirakan terjadi pada minggu t

Kas awal minggu t+1 = kas akhir minggu t

Headroom skenario = minimum sepanjang horizon
                   (kas akhir t - kas dibatasi penggunaannya t - batas cadangan t)

Ruang tambahan untuk kampanye = maksimum belanja tambahan pada jadwal tertentu
                               yang menjaga headroom skenario konservatif >= 0
```

Headroom dapat negatif dan tetap ditampilkan sebagai kekurangan; jangan dipotong menjadi nol. Angka “ruang tambahan” juga dibatasi budget yang disetujui dan kapasitas pelaksanaan. Kewajiban yang sudah masuk pembayaran forecast tidak dikurangkan lagi sebagai cadangan yang sama. Dana yang memang dibatasi kontrak/hukum dicatat terpisah dari cadangan manajemen. Saldo pendapatan ditangguhkan bukan otomatis cadangan kas sebesar nilai penuh; kebutuhan biaya pemenuhan layanan dan potensi refund harus dipetakan per kewajiban.

Pemeriksaan affordability juga melihat kewajiban material di luar 13 minggu, khususnya kursus yang sudah dibayar untuk layanan beberapa bulan kemudian. Simpan jadwal lanjutan atau cadangan pemenuhan yang disetujui untuk bagian yang belum masuk horizon. Jika kewajiban material atau biayanya masih unknown, angka “ruang tambahan yang aman” tidak diterbitkan. Cadangan di luar horizon dan pengeluaran yang masuk forecast harus saling menggantikan saat horizon bergeser, bukan dihitung ganda.

| Input forecast | Penanganan |
|---|---|
| Saldo pembuka | Per rekening, cutoff sama, bukti statement, rekonsiliasi dan jurnal awal yang disahkan. Jika belum ada, hanya boleh simulasi hipotetis berlabel. |
| Pesanan mitra baru | Jadwal penerimaan sebelum tes sesuai kondisi pesanan; jangan memakai pola kredit 30 hari untuk reseller prabayar. |
| AR belum tertagih | Hanya residual yang belum dibayar, tanggal ekspektasi dan dasar probabilitas. Uang yang sudah masuk saldo awal tidak diramal masuk lagi. |
| Uang muka yang sudah diterima | Tidak menjadi inflow masa depan; kewajiban pemenuhan/refund tetap masuk jadwal pengeluaran. |
| Tagihan dan biaya layanan | Residual pembayaran IIEF, biaya per jadwal, kontrak, vendor quote; approved payable menggantikan estimasi terkait, bukan ditambahkan. |
| Marketing | Pembayaran terjadwal dari komitmen/bill; bedakan spend layanan dengan isi ulang saldo iklan. |
| Pajak | Hanya kewajiban dan skenario dari Tax yang telah ditinjau. Unknown tetap unknown dan menjadi sensitivitas eksplisit. |
| Payroll, sewa, pinjaman, capex | Input schedule dengan bukti dan reviewer sampai modul pemilik tersedia. Modal/pinjaman terpisah dari arus kas operasi. |
| Transfer antar bank OSEE | Dua sisi disatukan; tidak meningkatkan kas perusahaan atau dianggap biaya marketing. |
| Refund, chargeback, pembatalan | Timing kas dan dampak pendapatan berbeda; link ke transaksi asal serta status persetujuan. |

Setiap cash item memiliki `economic_obligation_id` atau `economic_receipt_id`, asal, residual, tanggal, rentang tanggal bila belum pasti, status, dan pengganti/supersession. Pencegahan double count dilakukan pada identitas ekonomi, bukan hanya kesamaan nilai. Invoice IIEF Mei yang status pembayarannya belum diketahui tampil sebagai calon kewajiban yang perlu diperiksa; jangan menjadikannya otomatis utang jatuh tempo saat ini.

Untuk dua minggu terdekat, sediakan detail harian agar saldo akhir Jumat yang positif tidak menyembunyikan kekurangan kas pada Selasa. Setiap roll-forward menyimpan versi sebelumnya dan menjelaskan selisih forecast terhadap aktual: waktu, nilai, kewajiban terlewat, atau asumsi berubah.

Sediakan skenario **konservatif**, **rencana**, dan **optimistis**, dengan asumsi volume, harga, biaya IIEF, waktu penerimaan, refund, dan marketing. Probabilitas pipeline berasal dari cohort historis yang cukup atau input yang ditandai asumsi. Hindari label P10/P50/P90 sebelum model probabilistik dikalibrasi. Proyeksi berbobot bukan bukti uang akan tersedia tepat waktu.

Fungsi runway sederhana hanya ditampilkan jika burn rate positif, representatif, dan basis kas lengkap; pada bisnis musiman/prabayar utamakan grafik mingguan. Jangan menampilkan “runway tak terbatas” saat data biaya belum ada.

### 9. Marketing: dari pengeluaran menuju hasil bisnis

Marketing perlu domain data operasionalnya sendiri agar Direktur tidak mengandalkan angka yang diketik ulang di dashboard. Sumber awal dapat berupa CSV dengan template yang tervalidasi; otomatisasi menyusul setelah akun dan izin diketahui.

**Funnel langsung:** touchpoint → lead terverifikasi → kebutuhan produk → order → pembayaran → layanan → refund/repeat. Setiap tahap memiliki tanggal dan definisi masuk/keluar.

**Funnel mitra:** prospek mitra → penilaian/kontrak → order pertama dibayar → order berulang → layanan dan kontribusi. Akuisisi satu mitra tidak disamakan dengan akuisisi satu peserta; repeat order adalah pembelian mitra, bukan otomatis pelanggan baru OSEE.

**Funnel kursus:** lead → konsultasi/tes penempatan bila ada → enrolment → pembayaran → sesi → penyelesaian/perpanjangan. Tahap konsultasi bukan fakta operasi OSEE sampai dikonfirmasi.

Identitas pembeli, mitra, peserta, pembayar, dan lead dipisahkan. Pengaitan dilakukan dengan ID order/form/CRM yang stabil atau mapping ditinjau; nama mirip atau nomor WA bersama saja tidak cukup. Simpan `raw_source_label` agar 44 label rekap tidak hilang saat canonical mapping disahkan. Identifier pribadi tidak menjadi dimensi publik Direktur dan tidak dikirim ke AI untuk menghitung cohort. Jika sejarah pelanggan belum lengkap, gunakan label **pertama kali terlihat dalam data**, bukan pasti pelanggan baru. Pembeli lama yang membeli produk baru adalah baru untuk produk itu, bukan otomatis baru untuk OSEE.

**Asal akuisisi, cara komunikasi, model penjualan, dan petugas sales adalah dimensi terpisah.** Contoh: calon murid datang dari Google Ads, bertanya lewat WA, dilayani sales, lalu membayar OSEE. Ini satu konversi dengan beberapa touchpoint, bukan tiga penjualan untuk Google, WA, dan sales. Mitra dapat merupakan reseller sekaligus pihak yang pertama kali mengenalkan OSEE; jangan menganggap semua order reseller sebagai kanal akuisisi tunggal tanpa bukti.

| Metrik | Definisi dan penggunaan |
|---|---|
| Biaya iklan platform | Spend layanan yang dilaporkan platform pada timezone, mata uang, dan basis pajak sumber; masih perlu rekonsiliasi ke bill/ledger. |
| Biaya akuisisi penuh | Media + agency + creative + aktivitas penjualan yang ditetapkan dalam kebijakan alokasi; tampilkan beda dengan media-only. |
| Cost per lead | Biaya relevan / lead unik sesuai definisi dan periode/cohort; spam/test tidak dihitung. |
| CAC langsung | Biaya akuisisi cohort langsung / pembeli langsung baru yang memenuhi status pembayaran yang disetujui; bukan semua peserta. |
| Biaya aktivasi mitra | Biaya akuisisi mitra / mitra baru dengan order pertama dibayar dalam jendela matang. |
| ROAS platform | Conversion value versi platform / media spend versi platform; label **klaim atribusi platform**. |
| ROAS terhubung ERP | Nilai transaksi OSEE yang memenuhi status, jendela, dan pembobotan atribusi / media spend sebanding. Selalu nyatakan basis: booking, kas, atau pendapatan. |
| Rasio pendapatan terhadap marketing | Pendapatan perusahaan / seluruh biaya marketing pada basis periode konsisten; indikator campuran, bukan atribusi kausal. |
| Kontribusi cohort setelah akuisisi | Kontribusi unit/order cohort − biaya akuisisi cohort; membedakan omset tinggi dari hasil ekonomis. |
| Nilai pelanggan/mitra historis | Akumulasi kontribusi selama jendela observasi, dengan lama observasi; estimasi lifetime belum layak bila sejarah terlalu pendek. |
| Payback akuisisi | Periode ketika kontribusi kumulatif cohort menutup biaya akuisisi; versi kas dan akuntansi diberi label berbeda. |

Aturan atribusi awal: satu model versi aktif untuk satu tampilan, misalnya last eligible touch yang parameternya disetujui. Simpan juga first touch dan klaim platform sebagai sudut pandang berbeda. Setiap order line memiliki bobot atribusi total paling banyak 1 per model; sisanya **tidak diketahui/organik menurut bukti**, bukan dipaksa ke kampanye terbesar. Refund/kredit menyesuaikan basis yang sesuai. Jangan menjumlahkan seluruh conversion value Meta, Google, dan CRM.

`BusinessConversionDefinition` memetakan tujuan bisnis ke event sumber secara berversi dan disetujui: misalnya action type Meta atau conversion action/goal Google yang benar-benar berarti purchase pada setup OSEE. Simpan setting counting dan dedup yang digunakan. Lead, klik WA, add-to-cart, dan purchase tidak dijumlahkan menjadi satu angka penjualan; action/value yang tumpang tindih juga tidak dijumlahkan. Mapping baru membuat attribution run baru dan tidak mengubah snapshot historis tanpa restatement.

Simpan jendela atribusi, tanggal konversi vs tanggal klik, timezone, model version, status cohort, dan cut-off data. Hasil bulan lalu dapat berubah karena konversi terlambat; snapshot rapat tetap beku dan restatement dijelaskan.

Eksperimen marketing harus mempunyai hipotesis, populasi, kontrol/holdout bila layak, unit pembagian, periode, budget maksimum, metrik utama, guardrail margin/refund/kapasitas, dan aturan berhenti. Korelasi sebelum-sesudah atau ROAS tinggi tidak cukup untuk mengklaim dampak tambahan. Pada volume kecil, tampilkan hasil eksploratif dan keterbatasan, bukan angka uplift pasti.

### 10. Budget dan kendali belanja

Budget disusun per tahun/kuartal/bulan, tim, produk, kanal, kampanye, tujuan, dan cost center sesuai kebutuhan. Hindari hierarki terlalu rinci ketika source mapping belum mendukungnya.

**Keadaan yang wajib terpisah:** usulan, disetujui, komitmen terbuka, biaya sudah terjadi, tagihan, kas dibayar, saldo prabayar, forecast sisa, dan revisi. Satu belanja melewati tahap-tahap tersebut; bukan tujuh pengeluaran berbeda.

```text
Sisa otorisasi budget = budget versi disetujui
                       - biaya terjadi pada basis budget
                       - komitmen terbuka yang belum menjadi biaya
                       - reservasi aktif lain yang belum masuk dua kategori di atas

Perkiraan akhir periode = biaya terjadi
                         + estimasi seluruh biaya yang masih akan terjadi
                         (termasuk komitmen terbuka, dihitung sekali)

Selisih perkiraan = perkiraan akhir periode - budget disetujui
```

Pengeluaran yang belum memiliki bill dapat berupa estimasi spend tertunda; ketika bill disahkan, estimasi terkait diganti dengan bukti lebih kuat. Kolom tagihan dan pembayaran merupakan rekonsiliasi/status penyelesaian, tidak ditambahkan lagi ke biaya terjadi. Deposit ke akun iklan adalah arus kas/prepayment; pemakaian iklan menjadi spend. Kredit vendor dan refund harus menunjuk baris asal.

Contoh aritmetika hipotetis: budget Rp20 juta, biaya terjadi Rp6 juta, komitmen tersisa Rp4 juta → sisa otorisasi Rp10 juta. Jika dari biaya terjadi Rp6 juta baru Rp3 juta dibayar, sisa otorisasi tetap Rp10 juta; sisa kas pembayaran Rp3 juta mempengaruhi forecast kas.

Approval budget memeriksa: versi belum berubah, budget induk cukup, tanggal berlaku, konflik kepentingan, sumber data kas, batas cadangan, kapasitas pelaksanaan, dan risiko komitmen. Reservasi memiliki masa berlaku serta alur pelepasan; konversi reservasi menjadi komitmen mengurangi reservasi terkait secara atomik. Penambahan budget tidak otomatis mengizinkan pembayaran atau perubahan setelan iklan. Nilai dan syarat batas otorisasi ditetapkan direktur; angka contoh dalam rancangan bukan aturan aktif.

Untuk transaksi paralel, kunci baris budget/komitmen dan gunakan transaksi atomik agar dua persetujuan tidak memakai sisa yang sama. Bukti persetujuan merekam nilai, versi kebijakan, actor, waktu, idempotency key, dan hasil pemeriksaan.

Karena kampanye berbeda dapat memakai rekening perusahaan yang sama, tambahkan `LiquidityReservation` dan version check pada posisi likuiditas perusahaan. Pemeriksaan serta reservasi tambahan kas dilakukan atomik lintas budget: dua envelope berbeda tidak boleh masing-masing mengklaim headroom yang sama. Reservasi kas menunjuk jadwal dan obligation ID, lalu diganti oleh komitmen/pembayaran nyata sesuai residual agar tidak menjadi pengurangan kas berulang. Lock order perusahaan → budget → reservation dibuat konsisten untuk mengurangi deadlock.

### 11. Target, arah perusahaan, dan kebijakan

Sasaran memiliki horizon, alasan, metric key/version, baseline, target, guardrail, pemilik, dan asumsi. Contoh bentuk sasaran: “menaikkan kontribusi ITP sambil menjaga kecukupan kas dan kualitas layanan”; angka target belum ditetapkan sampai baseline layak.

Pohon sasaran: **arah tahunan → sasaran kuartalan → inisiatif → budget/kapasitas → tugas → evaluasi hasil**. Hindari hanya mengejar revenue: guardrail dapat mencakup margin, waktu tunggu pelanggan, ketepatan tes, beban pengajar, refund, dan konsentrasi mitra. Metrik kualitas membutuhkan data Operations/Academics, bukan tebakan AI.

| Jenis kebijakan | Data penilaian | Output dan pelaksana |
|---|---|---|
| Harga ITP untuk order mendatang | Versi biaya IIEF, kontrak mitra, kontribusi, sensitivitas volume | Usulan harga/tanggal; Finance/penjualan memeriksa kontrak dan menerapkan price version melalui layanan resmi. |
| Cadangan kas | Forecast konservatif, kebutuhan layanan, komitmen, risiko vendor | Batas dan cakupan cadangan berversi; Finance memantau, tidak mengubah saldo buku. |
| Alokasi marketing | Kontribusi, CAC, bukti eksperimen, kematangan cohort, kapasitas | Budget usulan per tujuan/kanal; marketing melaksanakan setelah otorisasi. |
| Pengembangan mitra | Aktivasi, repeat, kontribusi, konsentrasi, biaya dukungan | Program onboarding/retensi dan target pemilik akun. |
| Ekspansi produk/lokasi | Permintaan, kapasitas, unit economics, biaya tetap, titik impas, kas | Business case dan pilot dengan batas kerugian/biaya, bukan pembukaan otomatis. |
| Kepatuhan/pajak | Kesiapan data, kewajiban dan keputusan Tax yang ditinjau | Penugasan review atau rencana yang sah; tidak menetapkan tarif/kelayakan dari narasi AI. |

Setiap keputusan menyimpan masalah, fakta bersnapshot, opsi termasuk mempertahankan kondisi, asumsi, dampak keuangan/operasi, risiko, rekomendasi, owner, persetujuan, tanggal efektif, masa evaluasi, ukuran sukses, dan kondisi menghentikan/mengubah arah.

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> InReview: diajukan
    InReview --> Draft: perlu perbaikan
    InReview --> Rejected: ditolak dengan alasan
    InReview --> Approved: mandat dan pemeriksaan terpenuhi
    Approved --> Scheduled: jadwal pelaksanaan disahkan
    Scheduled --> Active: pelaksana mengonfirmasi versi berlaku
    Active --> UnderEvaluation: periode evaluasi
    UnderEvaluation --> Completed: hasil dan tindak lanjut dicatat
    Approved --> Superseded: revisi baru disetujui
    Active --> Superseded: kebijakan pengganti berlaku
    Active --> Suspended: penghentian disahkan
```

Tombol “Setujui” tidak berarti campaign sudah berjalan atau harga sudah aktif. Status pelaksanaan berasal dari modul pelaksana atau bukti staf berwenang. Tidak tersedia integrasi → tampilkan tugas implementasi manual dan bukti penyelesaian. Kebijakan historis tidak diedit; revisi membuat versi baru. Pembatalan arah tidak membatalkan transaksi/kontrak yang sudah terjadi tanpa workflow terkait.

Sebelum `Scheduled → Active`, layanan pelaksana memeriksa ulang exact proposal version, hak actor, masa berlaku persetujuan/reservasi, kesegaran data material, likuiditas lintas budget, kapasitas, dan kondisi kontrak. Perubahan material mengembalikan usulan ke review dengan alasan. Konfirmasi pelaksana saja tidak dapat melewati pemeriksaan ini.

### 12. Siklus kerja dan pemberitahuan

| Ritme usulan | Pengguna | Hasil |
|---|---|---|
| Harian, 5–10 menit | Direktur | Risiko kas, perubahan material, keputusan tertunda; tanpa kewajiban membaca semua grafik. |
| Mingguan | Finance + marketing + direktur | Forecast 13 minggu diperbarui, realisasi/komitmen direkonsiliasi, tindakan minggu berikutnya. |
| Bulanan | Direktur dan pemilik fungsi | Paket manajemen bersnapshot, variance aktual-budget, hasil kebijakan, revisi rencana. |
| Kuartalan | Direksi | Strategi produk/mitra/marketing, skenario, target dan kapasitas kuartal berikutnya. |

Ritme ini rekomendasi proses, bukan automasi yang telah dijadwalkan. In-app notification menjadi kanal awal. Email/WhatsApp/push hanya setelah konfigurasi penerima, izin, dan preferensi; detail keuangan tidak masuk pesan publik.

Alert memiliki `rule_version`, tingkat dampak, bukti, owner, tenggat, dedup key, cooldown, status acknowledged/resolved, dan waktu kedaluwarsa. Pisahkan **risiko bisnis** dari **data belum tersedia**. Contoh: “saldo bank belum disinkronkan” tidak boleh berubah menjadi “perusahaan kekurangan kas”. Alert duplikat dikumpulkan, lalu eskalasi mengikuti kebijakan yang disetujui.

### 13. AI Direktur: penasihat yang menggunakan bukti

AI mendukung empat pekerjaan: menjelaskan apa yang terjadi, menyusun hipotesis penyebab, membandingkan pilihan, dan menyiapkan draf keputusan. Mesin perhitungan aplikasi menetapkan angka; direktur menetapkan pilihan. AI dapat menantang asumsi dengan sopan, termasuk menyatakan belum ada dasar untuk menaikkan budget.

Contoh pertanyaan yang harus didukung:

- “Apa tiga masalah yang paling perlu saya tangani minggu ini?”
- “Peserta ITP berubah; apakah karena volume, harga, mitra, atau data yang belum lengkap?”
- “Kalau biaya IIEF naik, kelompok harga mitra mana yang perlu ditinjau?”
- “Bandingkan tambahan budget Meta, Google, SEO, dan program mitra dari sisi kontribusi dan kas.”
- “Apakah banyak lead WA gagal menjadi pesanan, dan informasi apa yang kita perlukan untuk mengetahui penyebabnya?”
- “Buat draf rencana 90 hari, dengan target yang dapat diukur dan batas pengeluaran.”
- “Apa yang masih kurang agar laporan perusahaan dan pajak dapat ditinjau?”

Pertanyaan pajak meneruskan ke fakta kesiapan Tax dan sumber resmi yang telah ditinjau. Tidak ada model bahasa yang menetapkan sendiri tarif, kelayakan fasilitas, angka kewajiban, atau status pelaporan. Rencana pengurangan biaya pajak masa depan tetap memerlukan bukti, dasar hukum yang berlaku, dan workflow reviewer Tax.

#### 13.1 Alur satu analisis

```mermaid
sequenceDiagram
    actor U as Direktur
    participant W as UI dan otorisasi Django
    participant G as Gateway konteks dan izin pengiriman
    participant T as Tools analitik terdaftar
    participant R as OpenRouter dan endpoint disetujui
    participant V as Validator bukti dan angka
    U->>W: Pertanyaan dan periode
    W->>W: Periksa perusahaan, izin, quota
    W->>G: Intent, parameter, profil pengiriman
    G->>T: Ambil snapshot dan kesiapan data
    T-->>G: Fakta agregat dan referensi lokal
    G->>G: Minimalkan pertanyaan, history, dan hasil tool
    G->>R: Konteks yang diizinkan
    R-->>G: Permintaan tool atau jawaban terstruktur
    G->>T: Validasi dan jalankan tool baca-saja bila perlu
    T-->>G: Hasil deterministik
    G->>R: Hasil tool yang lolos kebijakan
    R-->>V: Klaim, fakta pendukung, opsi, keterbatasan
    V->>V: Periksa scope, referensi, label, angka
    V-->>W: Jawaban tervalidasi atau ringkasan lokal
    W-->>U: Penjelasan, sumber, dan usulan
    U->>W: Simpan sebagai draf keputusan
```

Tidak semua pertanyaan memerlukan tool loop. Gunakan pengambilan data sekali dan template lokal untuk ringkasan rutin. Orkestrator membatasi putaran tool, rentang tanggal, jumlah hasil, token, durasi, dan biaya sebelum memanggil provider.

#### 13.2 Tiga izin yang tidak boleh disamakan

1. **Boleh membaca:** apakah pengguna dapat melihat angka atau dokumen di ERP.
2. **Boleh mengirim:** apakah kategori informasi tersebut boleh diproses OpenRouter dan endpoint yang dipilih.
3. **Boleh bertindak:** apakah pengguna boleh mengusulkan atau menyetujui tindakan bisnis.

Hak melihat seluruh keuangan tidak otomatis mengizinkan pengiriman semua dokumen ke provider. Konfigurasi chat pajak saat ini hanya mengirim topik publik; flag lama tidak boleh diam-diam mengaktifkan data privat Direktur.

Profil awal yang diusulkan untuk analisis privat: metrik agregat, rentang waktu, kualitas data, ID mitra samaran jika dibutuhkan, dan asumsi skenario. Bank account number, NPWP/NIK, kontak peserta, identitas siswa, kredensial, PDF mentah, OCR, dan catatan internal bebas tidak dikirim. Agregat tetap informasi rahasia perusahaan; data kelompok kecil atau kombinasi pertanyaan dapat membuka identitas, sehingga perlu batas pengelompokan dan akses.

Filter harus meliputi **pertanyaan pengguna, riwayat percakapan, hasil tool, ringkasan, dan konteks error**. Untuk rilis pertama gunakan intent lokal dan parameter terstruktur. Pertanyaan bebas yang tidak dapat diminimalkan secara andal menghasilkan pratinjau pertanyaan yang sudah disanitasi atau permintaan penjelasan lokal sebelum dikirim. Jangan mengirim pertanyaan asli ke model lain sekadar untuk memeriksa apakah pertanyaan itu sensitif.

Percakapan privat bagi pembuatnya secara default. Berbagi ke finance/rapat adalah tindakan eksplisit yang menghasilkan salinan atau snapshot dengan izin penerima, bukan membuka seluruh history pribadi. Pencabutan akses berlaku pada tool berikutnya, hasil job yang selesai belakangan, cache, dan unduhan lama.

#### 13.3 Tool yang boleh dipanggil AI

| Tool | Parameter terkontrol | Output |
|---|---|---|
| `get_data_readiness` | Periode, domain enum | Sumber, cakupan, kesegaran, pengecualian, data yang kurang |
| `get_reported_activity` | Periode, grouping enum | Rekap dilaporkan dengan label asli dan batasnya |
| `get_financial_metrics` | Snapshot, metric keys dalam allowlist, filter berizin | Nilai ledger/agregat, formula version, lineage |
| `get_cash_forecast` | Forecast version, scenario ID | Cash items agregat, headroom, asumsi, input yang belum lengkap |
| `get_budget_position` | Periode, cost center/kampanye berizin | Budget, biaya, komitmen, reservasi, pembayaran yang direkonsiliasi |
| `get_marketing_funnel` | Produk, cohort, kanal, model attribution | Tahap, biaya, hasil, lag, denominator dan coverage |
| `get_partner_health` | Periode, top-N terbatas | Aktivasi, repeat, konsentrasi, kontribusi yang dapat dihitung |
| `compare_scenario` | Jenis simulasi allowlist, asumsi tervalidasi | Output mesin hitung dan perubahan dibanding baseline |
| `get_decision_outcomes` | Keputusan yang dapat dibaca | Sasaran, tugas, hasil, bukti dan keterbatasan atribusi dampak |
| `get_tax_readiness` | Tahun/periode | Status dan bukti yang kurang dari Tax, tanpa aturan pajak buatan |
| `get_fact_evidence` | Fact IDs yang sudah diterbitkan pada run ini | Metadata sumber dan tautan privat yang dibuat server |

Organization ID berasal dari sesi terotorisasi, tidak diterima dari argumen model. Wrapper memvalidasi objek, izin, periode, ukuran hasil, dan profile pengiriman sebelum data keluar. Tidak ada tool SQL bebas, Python/shell, pencarian file sewenang-wenang, URL fetch buatan model, perubahan harga, pembayaran, atau pengiriman WA/email. Tool calling adalah permintaan dari model; aplikasi tetap pelaksana yang menegakkan batas. [Dokumentasi tool calling OpenRouter](https://openrouter.ai/docs/guides/features/tool-calling).

Semua tool analitis mengacu ke `DirectorRun.snapshot_manifest` yang dibekukan server, termasuk tool yang parameternya hanya berupa periode. Jangan membaca `current_snapshot` terbaru secara diam-diam di tengah run. Perbandingan antar snapshot diperbolehkan hanya jika kedua versinya eksplisit. Hak akses dan kebijakan pengiriman tetap dicek pada keadaan terbaru; data analitis yang beku tidak berarti hak akses juga beku.

#### 13.4 Kontrak jawaban dan bukti

Jawaban ditampilkan sebagai: **kesimpulan → fakta pendukung → dugaan penyebab → pilihan → dampak dan asumsi → risiko → tindakan yang disarankan → kapan ditinjau lagi**. Setiap bagian boleh menyatakan tidak cukup data. Dugaan penyebab tidak diberi label fakta.

```json
{
  "run_id": "server-issued-id",
  "summary_claims": [
    {"kind": "observation", "fact_ids": ["fact-issued-by-server"], "explanation": "Teks penjelasan"}
  ],
  "hypotheses": [{"explanation": "Dugaan", "evidence_needed": ["Data yang dibutuhkan"]}],
  "options": [{"scenario_result_id": "validated-result-id", "tradeoffs": ["Dampak"]}],
  "proposals": [{"type": "budget_review", "rationale_fact_ids": ["fact-issued-by-server"]}],
  "limitations": ["Batas kesimpulan"],
  "next_intents": ["compare_scenario"]
}
```

Ini sketsa kontrak, bukan respons nyata. Schema produksi menetapkan enum, batas panjang, field wajib, dan menolak field tambahan. Kartu angka di-render langsung dari fact ID oleh server. Semua klaim kuantitatif observasi/perhitungan—rupiah, peserta, conversion rate, persentase, rasio, dan durasi—harus merujuk fakta/hasil hitung terdaftar. Angka usulan seperti target atau kenaikan budget adalah parameter bertipe yang ditandai **asumsi/usulan**, bukan fakta. Penomoran paragraf biasa tidak termasuk klaim metrik. Validasi juga memeriksa basis label: fakta rekap tidak cukup untuk klaim laba meskipun nilai dan ID cocok.

JSON Schema membantu format respons, tetapi dukungan/enforcement berbeda antar endpoint dan tidak membuktikan kebenaran finansial. Pasangan model-provider harus diuji dengan schema dan tools yang benar-benar dipakai. [Structured outputs OpenRouter](https://openrouter.ai/docs/guides/features/structured-outputs).

Prompt injection dari nama kampanye, label mitra, PDF, atau chat diperlakukan sebagai isi data yang tidak berwenang. Instruksi di dalamnya tidak dapat mengganti izin atau policy. Tautan bukti dibuat server; output di-escape; tidak merender HTML bebas atau URL unduhan dari model. Tidak menyimpan chain-of-thought; audit menyimpan fakta, alasan yang ditampilkan, tool calls, versi dan hasil validasi.

#### 13.5 OpenRouter, privasi, biaya, dan kegagalan

Gunakan kredensial backend khusus analisis Direktur, model/endpoint yang lolos evaluasi, dan konfigurasi routing tertutup. Contoh bentuk kebijakan:

```json
{
  "model": "<model-yang-lolos-evaluasi>",
  "provider": {
    "only": ["<endpoint-provider-yang-disetujui>"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "data_collection": "deny",
    "zdr": true
  }
}
```

Slug provider umum dapat mencakup varian/region; pilih endpoint penuh bila batas itu diperlukan dan tersedia. `max_price` membatasi harga token yang memenuhi routing, bukan total biaya satu analisis. Jangan melonggarkan routing otomatis ketika endpoint gagal. Ini rekomendasi konfigurasi, belum koneksi aktif. [Provider routing OpenRouter](https://openrouter.ai/docs/guides/routing/provider-selection).

ZDR tidak berarti tidak ada pemrosesan, jaminan lokasi Indonesia, atau tidak ada metadata; dokumentasinya juga membedakan caching tertentu. Kebijakan pengiriman harus diperiksa bersama endpoint sebenarnya. Nonaktifkan logging isi privat dan broadcast secara default. [Zero Data Retention](https://openrouter.ai/docs/guides/features/zdr), [data collection](https://openrouter.ai/docs/guides/privacy/data-collection), [input/output logging](https://openrouter.ai/docs/guides/features/input-output-logging).

`AIUsageReservation` mencadangkan biaya maksimum yang dihitung dari batas token/putaran dan harga endpoint yang disetujui sebelum setiap request. Reservasi atomik per perusahaan/pengguna/global; batasi jumlah request paralel. Setelah respons, rekonsiliasi dengan usage/cost provider. Timeout tidak berarti biaya nol: simpan reservasi belum terselesaikan sampai dapat diperiksa. Biaya API AI dicatat terpisah dari budget iklan dan masuk cost center yang ditetapkan Finance.

Kontrol lokal tetap diperlukan meskipun memakai limit provider. Dokumentasi OpenRouter menyatakan workspace budgets tersedia untuk Enterprise dan request yang sedang berjalan dapat melampaui batas nominal. Jangan mengasumsikan fitur itu ada di akun OSEE. [Workspace budgets](https://openrouter.ai/docs/guides/features/workspaces/workspace-budgets).

Tanpa key, izin pengiriman, budget, endpoint cocok, atau respons valid, tampilkan **Ringkasan dari data aplikasi** dan kalkulator deterministik. Jangan mengklaim AI berhasil. Tangani refusal, error envelope pada respons, JSON terpotong, tool invalid, sumber palsu, dan stale snapshot. Satu retry terkontrol hanya jika aman dan masih dalam batas biaya; tidak melakukan retry dengan provider yang lebih longgar.

Inference mempunyai catatan `AIInvocation` persisten per run/round/attempt: `reserved → dispatching → completed` atau `outcome_unknown`. Jika worker mati/timeout sesudah request mungkin terkirim, job yang diambil ulang tidak boleh otomatis mengirim inference yang sama. Pertahankan reservasi biaya, rekonsiliasi bila provider menyediakan bukti, atau akhiri dengan fallback lokal. Idempotency lokal tidak menjamin dedup di OpenRouter. Retry otomatis hanya untuk kegagalan yang terbukti terjadi sebelum pengiriman; percobaan baru setelah hasil tidak diketahui harus mengikuti keputusan eksplisit dan budget baru.

### 14. Mesin simulasi dan perencanaan

Simulasi menyimpan input immutable, snapshot baseline, rumus/version, tanggal, actor, asumsi, dan result hash. UI boleh mengubah draft; hasil lama tetap terikat input lama. Perhitungan keuangan memakai Decimal; urutan pembulatan terdokumentasi, nilai uang dikirim sebagai string desimal. Persentase dan kuantitas memiliki unit eksplisit.

| Simulasi | Input | Output untuk direktur |
|---|---|---|
| Perubahan biaya IIEF | SKU, biaya baru, tanggal efektif, pesanan/kuota yang belum terkunci, biaya tambahan | Dampak kontribusi dan jadwal kas per kelompok harga; order lama yang terikat tetap terpisah |
| Perubahan harga mitra | Harga usulan, kontrak, tanggal, asumsi volume/rentang respons | Titik impas volume, kontribusi, risiko kehilangan order, tanggal review |
| Tambahan budget kanal | Jadwal spend, biaya per akuisisi/rentang, lag konversi, kapasitas, kontribusi unit | Kebutuhan kas, tambahan hasil asumtif, kondisi merugi, maksimum pilot |
| SEO dan sales | Biaya konten/tim, kapasitas follow-up, asumsi lead, lag hasil | Horizon manfaat yang lebih panjang dan milestone awal; tidak dipaksa ROAS iklan harian |
| Mitra besar berhenti memesan | Mitra canonical, residual demand, biaya tetap/variabel, komitmen | Konsentrasi risiko, tekanan kontribusi dan kas, tindakan diversifikasi |
| Membuka kelas/produk baru | Kapasitas, harga, biaya pengajar/operasi, minimal enrolment, timing pembayaran | Unit impas, kebutuhan kas, sensitivitas pengisian, batas pilot |

Contoh hipotetis ITP: harga jual Rp520.000, biaya IIEF Rp450.000, biaya variabel lain Rp10.000 → kontribusi sebelum akuisisi Rp60.000/unit. Jika biaya IIEF menjadi Rp470.000, kontribusi menjadi Rp40.000/unit pada volume tetap. Untuk mempertahankan kontribusi total perlu volume 1,5 kali, sebelum perubahan biaya tetap, kapasitas, atau permintaan. Angka ini demonstrasi rumus; bukan hasil laba OSEE atau rekomendasi harga.

Titik impas tambahan belanja marketing dapat dihitung sebagai belanja tambahan dibagi kontribusi tambahan per unit yang positif, dibulatkan ke atas. Jika kontribusi nol/negatif, tidak ada jumlah unit positif yang menutup belanja dengan asumsi itu. Waktu pembayaran dan penerimaan tetap diuji secara terpisah.

Tambahan akuisisi **teratribusi** harus dibedakan dari tambahan akuisisi **yang benar-benar disebabkan intervensi**. Skenario memuat asumsi pembelian organik yang bergeser ke iklan, brand/retargeting, dan perpindahan antar kanal atau produk. `Kontribusi incremental setelah marketing = perubahan kontribusi sebelum marketing − perubahan spend marketing`; estimasi perubahan laba operasi juga mengurangi perubahan biaya tetap terkait. Jika klaim atribusi paid naik tetapi kontribusi bisnis total tetap, tambahan spend menurunkan hasil ekonomis. Average CAC/ROAS historis tidak langsung menjadi estimasi hasil marginal budget baru.

Jangan meramal respons volume harga/budget dengan hubungan linear otomatis. Cold start memakai asumsi direktur/finance dengan rentang. Model statistik baru dipertimbangkan setelah data cukup, diuji pada periode yang tidak dipakai melatih, dibanding baseline sederhana, dan dipantau error per produk/kanal. Model yang tidak mengungguli baseline atau tidak stabil tidak dipakai untuk rekomendasi belanja.

### 15. Integrasi enam kanal dan Finance

Kanal yang dipakai OSEE telah dikonfirmasi; akses teknisnya belum. Tabel ini merupakan kontrak target dan urutan onboarding, bukan daftar koneksi aktif.

| Sumber | Data minimum yang dicatat | Cara integrasi dan batas |
|---|---|---|
| **Mitra** | ID canonical, kontrak/tier, lead origin bila diketahui, order, pembayaran, layanan, repeat, biaya dukungan | Mulai dari Party + order Finance dan mapping rekap; portal mitra menyusul. Harga jual lanjutan reseller tetap di luar omzet OSEE. |
| **Meta Ads** | Account/campaign/ad IDs, tanggal, spend, impressions/clicks, actions/value menurut platform, setting atribusi, currency/timezone | Konektor reporting Insights atau file export. Izin minimum yang cukup, field/version diuji pada akun nyata. Tidak mengubah ads/budget. |
| **Google Ads** | Customer/campaign IDs, tanggal, cost, clicks, conversion metrics/value, model/jendela, currency/timezone | Reporting API dengan query terdaftar. Verifikasi developer token, OAuth, akses account, version, dan quota sebelum aktif. |
| **WhatsApp** | Lead/conversation ID internal, waktu inquiry/respons, status follow-up, owner, order link, sumber awal bila terbukti | Pastikan apakah WA Business app, Cloud API, atau BSP. Mulai input/CSV dan linking first-party; jangan menjanjikan akses riwayat chat dari nomor biasa. Jika API tersedia, adaptor menerima event yang diizinkan dan diverifikasi. |
| **SEO/website** | Landing page, kelompok query bila tersedia, klik/impression pencarian, form events, order links, biaya konten/agency | Search Console reporting, opsional GA4 bila terpasang, dan form/checkout website first-party. Search traffic bukan omzet. |
| **Sales** | Lead owner, stage history, aktivitas relevan, outcome, lost reason, order link, biaya tim/alokasi | Form CRM minimal di `marketing` atau adaptor CRM yang digunakan; jangan menciptakan komisi bila skema belum ditentukan. |
| **BNI/Finance** | Statement/cutoff, transaksi, settlement allocations, saldo awal, invoice/bill/journal | Konektor bank hanya baca pada tahap ini; ledger tetap Finance; direktur melihat waktu sinkronisasi dan rekonsiliasi. |

Meta mempublikasikan contoh Insights berisi spend, clicks, impressions, actions, dan action values dalam koleksi API resminya. Halaman developer utama terkena rate limit saat penelitian; gunakan koleksi resmi sebagai rujukan bentuk reporting dan verifikasi ulang endpoint/izin saat implementasi. [Contoh Insights oleh Meta](https://www.postman.com/meta/facebook-marketing-api/request/u07tack/get-ad-insights-l1).

Google Ads mendukung reporting melalui `GoogleAdsService.Search`/`SearchStream`. Ini kemampuan platform, bukan bukti akun OSEE sudah memiliki izin. [Google Ads reporting](https://developers.google.com/google-ads/api/docs/reporting/overview).

Search Console menyediakan query traffic menurut dimensi, tetapi tidak menjamin semua baris dikembalikan dan tanggal pelaporannya menggunakan Pacific Time. Simpan timezone dan coverage sumber; jangan memaksa agregat hari tersebut menjadi tepat satu hari WIB. GA4 Data API dapat menjadi sumber report tambahan jika property OSEE tersedia. [Search Console query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query), [GA4 Data API](https://developers.google.com/analytics/devguides/reporting/data/v1/basics).

Simpan ID kampanye, bukan hanya nama yang dapat berubah. Query string/UTM tidak berisi nama, email, nomor telepon, atau identitas peserta. Website menghasilkan correlation ID acak untuk form → lead → order. WA link/reference boleh menyimpan token acak yang aman jika alur mendukung; adanya klik WA saja tidak membuktikan percakapan atau pembayaran terjadi. Jangan mengirim daftar kontak sebagai audience atau offline conversion tanpa desain dan otorisasi tersendiri.

**Kontrak adapter minimum:** account allowlist, mode baca, `fetch_window`, pagination/cursor, watermark, source timezone/currency, source schema version, request ID, response hash, retry policy, quota, expected account-days, dan `last_success_at`. Time window ditetapkan per provider; data attribution yang dapat berubah diambil ulang dalam jendela terkontrol lalu dibandingkan. Data historis yang API tidak menyediakan tidak dipalsukan dari ekspor terbaru.

Untuk spend non-IDR, simpan mata uang sumber, amount native, FX rate/date/source, dan amount IDR. Cash settlement menggunakan transaksi Finance aktual; selisih kurs/fee tidak disembunyikan dalam conversion metrics. Rate/biaya pajak tidak ditentukan adapter marketing.

### 16. Model data dan granularitas

Semua tabel bisnis memiliki `organization_id`, ID stabil, created/updated metadata sesuai sifatnya, dan foreign key terlingkup. Hindari satu tabel JSON untuk semua objek; JSON hanya untuk payload sumber/kontrak yang berversi, bukan menggantikan constraint penting.

| Kelompok/entitas | Grain dan field utama | Invariant |
|---|---|---|
| `SourceConnection`, `SyncRun` | Satu koneksi/account; satu run/window: status, cursor, timezone, hash, coverage, error code | Credential reference terpisah; tidak mencampur account berbeda. |
| `SourceObservation` | Satu rekaman sumber berversi, natural key dan ingestion run | Reimport identik idempotent; revisi sumber membuat versi/supersession. Arsip `imports` tetap digunakan untuk PDF. |
| `DimensionMapping` | Source label/ID → Party/product/channel, reviewer, effective period | Riwayat mapping tidak menimpa label asli; overlap mapping dilarang jika ambigu. |
| `Lead`, `LeadStageEvent` | Satu prospek dan event tahap, owner, source, waktu, definition version | History tahap append-only; pembeli dan peserta adalah objek berbeda. |
| `Touchpoint`, `IdentityLink` | Satu interaksi dan link berbukti antar lead/order/contact | Tidak merge otomatis berdasarkan kemiripan nama. |
| `Campaign`, `CampaignDailyFact` | Campaign master; satu source/account/date/level/dimension set | Aggregate campaign/ad/adset tidak dijumlahkan bertumpuk. Report grain eksplisit. |
| `SpendReconciliation`, `SpendAllocation` | Hubungan nilai spend provisional ke bill/journal/campaign | Total allocation tidak melebihi source amount; residu dan selisih terlihat. |
| `BusinessConversionDefinition`, `AttributionRun`, `AttributionAllocation` | Mapping tujuan bisnis; model/snapshot; satu order line × touchpoint/unknown share | Tujuan/event tidak tumpang tindih; bobot eligible line termasuk unknown tepat 1 saat final; klaim platform tetap terpisah. |
| `MetricDefinition`, `MetricSnapshot` | Definisi version; satu metric × dimension set × period × basis × snapshot run | Rumus allowlist; `value=null` bukan 0; lineage dan missing inputs wajib. |
| `SnapshotManifest`, `DataQualityIssue` | Cutoff/vector source revisions; satu masalah berbukti dan owner | Snapshot publikasi tidak berubah; unresolved issues ikut turun ke hasil. |
| `CashPosition`, `ForecastVersion`, `ForecastLine` | Saldo bertanggal; versi horizon; satu arus kas tersisa | Satu obligation/receipt direpresentasikan sekali per skenario, aktual mengganti estimasi terkait. |
| `BudgetPlan`, `BudgetVersion`, `BudgetLine` | Versi envelope; satu periode × scope biaya | Approved version immutable; jumlah child/reservasi tidak melebihi kapasitas yang diotorisasi. |
| `BudgetReservation`, `LiquidityReservation`, `Commitment` | Reservasi budget/kas bertenggat dan komitmen ekonomi residual | Reservasi kas lintas budget memeriksa kapasitas perusahaan; state transition mengganti konsumsi sebelumnya secara atomik. |
| `Scenario`, `ScenarioResult` | Input, baseline, formula version, actor; output/hash | Hasil untuk input lama tidak disajikan sebagai hasil input baru. |
| `Objective`, `KeyResult`, `Initiative` | Sasaran, metrik/target, program pelaksanaan | Baseline/target snapshot version dan guardrail wajib. |
| `Decision`, `DecisionVersion`, `Approval` | Identitas keputusan; versi proposal; persetujuan actor/version | Approval mengikat versi persis; perubahan material memerlukan review baru. |
| `PolicyVersion`, `ExecutionTask`, `OutcomeReview` | Aturan scope/tanggal; pelaksanaan; hasil terhadap baseline | Tidak ada policy aktif bertabrakan tanpa precedence; rollback membuat versi pengganti. |
| `Experiment`, `ExperimentObservation` | Desain/assignment; outcome dan eligibility | Desain awal dibekukan; analisis ulang berversi dan maturity terlihat. |
| `DirectorConversation`, `DirectorRun`, `AIInvocation`, `AIUsageReservation` | Conversation private; run terikat snapshot; invocation state; reservasi biaya | Tidak bocor lintas scope; uncertain dispatch tidak direplay otomatis; model bukan actor approval. |

Metadata tambahan untuk facts: `occurred_at`, `effective_date`, `recorded_at`, `source_period`, dan `time_precision` (exact/day/month/ambiguous). Timestamp kejadian disimpan UTC dengan timezone sumber; tampilan WIB. Data hanya bulanan tidak dipecah menjadi tanggal harian buatan.

Model order, order line, cohort kursus, dan event tahap sales dalam rancangan ini adalah target kontrak; tidak semuanya sudah tersedia. Invoice Finance saat ini berisi satu produk/quantity. Adaptor awal boleh mengeluarkan satu referensi line stabil dari invoice itu, tetapi status invoice tidak boleh otomatis mengarang tanggal order diterima, lead source, atau penyelesaian yang tidak ada. Saat order terpisah dan multi-line/bundle dibangun, migrasi referensi mempertahankan linkage; total alokasi nilai bundle tidak melebihi total transaksi.

Uang menggunakan Decimal dengan currency eksplisit, tidak float JavaScript sebagai sumber hitungan. Quantities peserta berupa integer; jam layanan dan bobot atribusi memakai precision tersendiri. Composite constraints dan layanan scoped mencegah FK lintas perusahaan. Optimistic version check dipakai untuk draft UI; locking dan unique constraints untuk persetujuan/reservasi.

### 17. API, layanan, dan event

Endpoint berikut rancangan internal; semua meminta session auth/capability dan CSRF untuk mutasi browser. Filter organisasi ditentukan server. REST API publik bagi pihak ketiga, jika diperlukan kemudian, menggunakan kredensial/scopes terpisah.

| Endpoint usulan | Perilaku |
|---|---|
| `GET /api/v1/director/overview` | Mengembalikan snapshot, kartu, readiness, perhatian, dan decision queue. |
| `GET /api/v1/director/metrics/{key}` | Filter allowlist; hasil + rumus/lineage/coverage, tanpa SQL bebas. |
| `GET /api/v1/director/cash-forecasts/{id}` | Versi proyeksi, skenario, sumber, asumsi, granularity harian/mingguan. |
| `POST /api/v1/director/scenarios` | Membuat draft input terkontrol; tidak mengubah data transaksi. |
| `POST /api/v1/director/scenarios/{id}/calculate` | Job hitung dengan version check dan hasil immutable. |
| `POST /api/v1/director/budgets/{id}/submit` | Membekukan versi untuk review; memeriksa readiness kebijakan. |
| `POST /api/v1/director/decisions/{id}/approve` | Memeriksa mandat, exact version, expiry, dependencies; merekam approval, bukan menjalankan bank/ads. |
| `POST /api/v1/director/decisions/{id}/record-execution` | Actor pelaksana dan bukti; memperbarui state sesuai domain acknowledgement. |
| `POST /api/v1/director/conversations/{id}/runs` | Enqueue AI run dengan scope dan reservasi; respons job ID. |
| `GET /api/v1/director/runs/{id}` | Status/result terotorisasi; tidak bocor provider error atau prompt mentah. |
| `POST /api/v1/director/management-packs` | Membuat PDF/CSV snapshot privat sesuai izin, tercatat di audit. |
| `GET /api/v1/director/data-readiness` | Coverage, masalah, data owner, dan langkah pengisian. |

Mutasi memiliki idempotency key. Request dengan versi draft lama mengembalikan konflik, tidak menimpa perubahan orang lain. Hasil besar dipaginasi; ekspor dan job asynchronous dibatasi ukuran/durasi. Cache key mencakup organisasi, scope/permission version, snapshot ID, formula version, dan filter; jangan cache hanya berdasarkan URL.

Contoh **hasil tersedia tetapi belum terverifikasi**, memakai nilai sumber yang sudah diimpor:

```json
{
  "metric_key": "itp.source_reported_amount",
  "definition_version": 1,
  "value": "3564480000.00",
  "currency": "IDR",
  "basis": "source_reported",
  "period_start": "2026-01-01",
  "period_end": "2026-09-30",
  "coverage": "partial",
  "last_populated_source_date": "2026-09-02",
  "review_status": "exceptions_open",
  "limitations": ["Bukan pendapatan diakui", "September parsial", "Tanggal tertentu ambigu"],
  "snapshot_id": "<server-issued-id>",
  "source_refs": ["<private-source-id>"]
}
```

`period_end` di atas adalah batas label periode, bukan klaim data tersedia sampai 30 September. `as_of`, waktu impor, dan sumber cutoff dicatat terpisah. Untuk `cash.available`, respons saat data tidak cukup adalah `value: null`, `status: unavailable`, disertai daftar kebutuhan, bukan angka rekap di atas.

Event yang dibutuhkan: `finance.invoice_issued`, `finance.receipt_allocated`, `finance.delivery_recorded`, `finance.bill_approved`, `finance.payment_allocated`, `finance.period_closed`, `imports.batch_imported`, `marketing.spend_synced`, `marketing.lead_stage_changed`, `director.budget_approved`, `director.decision_approved`, `director.policy_activated`, serta event refund/kredit ketika workflow itu tersedia. Nama ini adalah kontrak target; audit nama event yang sudah ada sebelum mapping agar tidak menerbitkan dua kejadian ekonomi yang sama.

Envelope: `event_id`, `organization_id`, `topic`, `schema_version`, `aggregate_type/id`, `aggregate_version`, `occurred_at`, `recorded_at`, `correlation_id`, `idempotency_key`, dan referensi sumber minimal. Payload tidak membawa kredensial atau dokumen privat penuh. Consumer analytics hanya mengubah read model, tidak mem-post jurnal.

### 18. Pipeline data, konsistensi, dan pemulihan

```mermaid
flowchart LR
    S[Sumber dan domain ERP] --> R[Run impor/sinkronisasi + arsip]
    R --> V[Validasi schema, identitas, waktu, currency]
    V --> Q[Antrean masalah data]
    V --> N[Fact terstandardisasi dengan basis sumber]
    N --> C[Rekonsiliasi dan alokasi]
    C --> A[Hitung metrik dan atribusi berversi]
    A --> M[Manifest snapshot konsisten]
    M --> D[UI, laporan, AI, simulasi]
    Q --> H[Review manusia]
    H --> N
```

1. **Capture:** simpan checksum sumber, natural key, account, schema version, waktu, dan coverage. Input rusak masuk quarantine, bukan langsung agregat.
2. **Normalize:** petakan produk/party/kanal dengan bukti; pertahankan original label, timezone, currency, konflik dan missing values.
3. **Reconcile:** hubungkan spend platform dengan bill/ledger; order dengan payment/delivery; biaya dengan unit layanan. Rekap dan jurnal tetap basis terpisah.
4. **Calculate:** Decimal dan definisi berversi menghasilkan agregat yang bisa dibangun ulang. Fakta terlambat memperbarui current view melalui run baru.
5. **Publish:** swap pointer `current_snapshot` hanya setelah seluruh bagian yang diwajibkan selesai. Manifest mencatat revision/watermark tiap sumber; tidak mengklaim semua provider memiliki cutoff yang sama.
6. **Preserve:** snapshot rapat dan keputusan tetap beku. Restatement menautkan versi lama/baru, alasan dan perubahan. Koreksi ledger mengikuti aturan Finance dan periode, bukan analytics backfill.

DomainEvent sekarang baru tabel outbox. Implementasikan dispatcher/worker, delivery table, retry/backoff, heartbeat, dead-letter queue, dan consumer dedup sebelum menyebut integrasi otomatis. Action domain dan outbox ditulis dalam transaksi yang sama. `on_commit` dapat membangunkan worker, tetapi event tahan lama harus tetap bisa diambil jika proses mati setelah commit; callback saja bukan pengiriman andal. [Transaksi Django](https://docs.djangoproject.com/en/5.2/topics/db/transactions/).

Rancangan awal cukup worker database dengan lease, batas batch, dan unique delivery `(event_id, consumer_name, consumer_version)`. Delivery paling sedikit sekali; handler wajib idempotent. Event dapat datang terlambat/berulang; consumer membandingkan aggregate version atau membangun ulang rentang terdampak. Tidak menjanjikan exactly-once lintas layanan/provider.

Lock transaksi diperlukan untuk reservasi budget, approval, serta klaim job. Uji konkurensi pada PostgreSQL; keberhasilan SQLite tidak membuktikan locking produksi. [Django select_for_update](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).

Jika konektor gagal, tampilan memakai snapshot terakhir dengan label stale. Tidak mengisi hari gagal sinkron dengan nol. Bila kualitas data turun atau kewajiban kas baru muncul, batalkan rekomendasi affordability yang kedaluwarsa; keputusan yang sudah disetujui ditandai perlu review sesuai materiality policy dan dicek ulang sebelum eksekusi.

### 19. Keamanan, privasi, dan audit

| Risiko | Kontrol yang harus dibangun |
|---|---|
| Data perusahaan/pengguna tertukar | Scope di query/service/tool, FK validation, permission-aware cache, pengujian lintas organisasi dan pemilik conversation. |
| Direktur menjadi superuser teknis | Capabilities bisnis terpisah dari administration; MFA dan step-up untuk persetujuan sensitif saat produksi. |
| Dokumen/kontak bocor ke AI | Disclosure profile, minimization lokal seluruh jalur konteks, token/identity map lokal, tidak ada raw-document tool. |
| Prompt injection | Tool allowlist, argumen tervalidasi, no arbitrary network/SQL, output escaping, sumber link dari server. |
| Kredensial terbaca frontend/log | Secret store/environment backend, credential references terenkripsi sesuai deployment, redaksi log, rotasi dan revocation. |
| File/CSV berbahaya | Batas ukuran, tipe/schema, quarantine, sanitasi formula CSV saat ekspor, private storage, pemindaian file pada produksi. |
| Keputusan dimanipulasi | Exact-version approval, optimistic check, atomic reservation, audit perubahan/override, segregation of duties. |
| Kebocoran ekspor/laporan | Auth pada generation/download, expiry, audit, watermark perusahaan/snapshot, tidak ada public link default. |
| Webhook palsu/ulang | Verifikasi signature sesuai provider yang dipilih, event ID dedup, replay control yang kompatibel retry sah. |
| Backup/log terlalu terbuka | Enkripsi dan akses terbatas, retention per kategori, restore test, audit akses operator. |

Tidak perlu mengirim isi chat WA atau data peserta untuk menampilkan funnel dan produktivitas sales agregat. Simpan tujuan pengumpulan dan aturan retensi data; ekspor audience, komunikasi otomatis, pemrosesan data anak, atau transfer lintas negara memerlukan review privasi sesuai pemakaian sebenarnya. Rancangan ini tidak menyatakan kepatuhan hukum selesai hanya karena ada kontrol teknis.

Retensi dipisah: catatan keuangan mengikuti kebijakan Finance yang ditinjau; raw marketing dan pesan diminimalkan; percakapan AI dapat dihapus sesuai kebijakan; keputusan/snapshot/audit memiliki jadwal tersendiri. Penghapusan isi privat tidak boleh merusak bukti transaksi yang wajib dipertahankan—simpan referensi/hash minimum yang dibenarkan kebijakan. Jangan menetapkan satu angka retensi untuk seluruh data tanpa review.

PostgreSQL row-level security dapat ditambahkan sebagai lapisan pertahanan ketika konfigurasi tenancy/session sudah diuji. RLS bukan pengganti scoped services; peran tertentu seperti pemilik tabel/superuser mempunyai perilaku bypass yang perlu ditangani. [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

### 20. Infrastruktur, operasi, dan biaya kepemilikan

```mermaid
flowchart TB
    U[Browser direktur dan tim] --> H[HTTPS / reverse proxy]
    H --> W[Django web: RBAC, UI, API]
    W --> DB[(PostgreSQL: domain, outbox, analytics)]
    W --> FS[(Penyimpanan bukti privat)]
    DB --> J[Worker dan scheduler terkelola]
    J --> DB
    J --> E[Konektor baca: bank, ads, SEO, CRM]
    J --> G[AI gateway: izin, minimization, usage]
    G --> OR[OpenRouter / endpoint disetujui]
    W --> MON[Metric operasi dan audit teredaksi]
    J --> MON
    DB --> BK[Backup terenkripsi dan uji pemulihan]
    FS --> BK
```

Ini target produksi. App saat ini berjalan lokal dengan SQLite; deployment produksi, worker, outbox delivery, dan konektor di atas belum tervalidasi. Pisahkan dev/test/production; fixture test sintetis, salinan sumber privat hanya pada lingkungan yang diotorisasi. Modul Direktur tidak boleh memperberat posting Finance dengan analisis panjang dalam transaksi HTTP.

Mulai dengan satu database PostgreSQL, satu web deployment, worker terpisah secara proses, penyimpanan dokumen privat, dan backup. Redis/queue broker dapat ditambahkan jika volume/reliability job mengharuskannya; jangan bergantung pada proses browser untuk scheduler. Warehouse/read replica dievaluasi setelah profiling menunjukkan beban analitik mengganggu Finance atau volume sumber membenarkannya.

Biaya bulanan harus dipisah: hosting web/database, worker, storage/backup, observability, API AI, dan biaya alat/BSP/CRM yang benar-benar dipakai. Estimasi rupiah belum ditetapkan karena provider, volume, retensi, dan kontrak tidak diketahui. AI memakai caching snapshot yang aman, ringkasan deterministik untuk laporan rutin, dan analisis mendalam on-demand untuk menjaga biaya.

| Ukuran operasi | Target rancangan awal; wajib diuji sebelum menjadi SLA |
|---|---|
| Dashboard | p95 ≤ 2 detik dari snapshot pada volume data/pengguna yang disepakati |
| Simulasi umum | p95 ≤ 3 detik untuk input bounded; skenario berat menjadi job |
| AI | Respons penerimaan job cepat; target jawaban ≤ 30 detik untuk analisis standar, timeout dan fallback terlihat |
| Event internal | Target lag ≤ 5 menit saat sehat; backlog dan oldest undelivered event dipantau |
| Marketing | Minimum refresh harian sebagai target awal; kesegaran mengikuti sumber dan quota, bukan klaim real-time |
| Kas | Update sesuai statement/API yang disepakati; timestamp terlihat; tidak ada label live tanpa bukti |
| Availability | Target bisnis awal 99,5% bulanan untuk aplikasi produksi, setelah hosting dan monitoring dipilih |
| Pemulihan | Usulan RPO ≤ 1 jam, RTO ≤ 4 jam dengan backup/PITR yang mendukung dan restore drill; bukan kemampuan local SQLite saat ini |

Monitor error sinkron, missing account-days, unmatched spend/order/cost, drift totals, snapshot lag, job retry/dead-letter, approval conflict, policy violation, AI refusal/invalid facts/privacy blocks, latency, dan biaya per run. Log tidak memuat key, nomor bank, raw PDF, atau prompt privat. Ada runbook untuk revoked token, provider outage, data salah, job macet, budget race, restore database, dan penonaktifan AI/konektor per perusahaan.

### 21. Paket rapat dan keterlacakan keputusan

Laporan rapat bulanan dihasilkan dari satu snapshot manifest: cakupan, metrik utama, bridge pertumbuhan/margin, kas/forecast, marketing/budget, mitra/kapasitas, target, risiko, dan keputusan/tugas. Setiap halaman menyebut periode, cutoff, serta status draft/review/final internal. “Final internal” tidak berarti laporan pajak resmi atau laporan keuangan diaudit.

Narasi AI opsional dan ditinjau; angka tetap dari mesin hitung. Paket dapat diunduh sebagai PDF dan tabel CSV dengan ID sumber. Rapat menghasilkan `DecisionVersion` dan `ExecutionTask`; evaluasi menyandingkan rencana awal dengan hasil serta perubahan keadaan. Tidak menilai sebuah kebijakan berhasil hanya karena angka membaik sesudah diterapkan; jika kausalitas belum dibuktikan, laporkan sebagai hasil observasi.

### 22. Acceptance criteria yang wajib diuji

Tabel ini merupakan rencana pengujian sebelum rilis, bukan daftar tes yang sudah dijalankan untuk fitur Direktur.

| ID | Skenario | Hasil yang wajib |
|---|---|---|
| D01 | Hanya rekap 2026 tersedia | Nilai Rp3.564.480.000 berlabel rekap; cash/revenue/profit/CAC/ROAS tidak diciptakan. |
| D02 | Count cetak 6.520 vs detail 6.521 | Keduanya dan selisih tampil; tidak ada perbaikan diam-diam. |
| D03 | September parsial, Okt–Des tidak ada | Tidak ada nol palsu atau klaim turun full-month/annual growth. |
| D04 | Annual-style page hanya Jan–Jun | Tidak ditambahkan lagi ke jumlah monthly Jan–Sep; coverage berbeda terlihat. |
| D05 | Tanggal Januari konflik dan tanggal merged | Unit dihitung satu kali; laporan harian yang membutuhkan tanggal pasti menolak atribusi tanggal palsu. |
| D06 | Satu mitra memesan 50 seat | Satu pembeli organisasi dan 50 unit, bukan 50 pelanggan baru. |
| D07 | 44 raw labels sebelum mapping | Tidak disebut 44 entitas mitra legal terverifikasi; mapping menyimpan asal. |
| D08 | Customer lama membeli produk lain | New-to-product terpisah dari new-to-company; histori tidak lengkap → first observed. |
| D09 | Mitra memiliki markup retail | Pendapatan OSEE tidak bertambah oleh penjualan lanjutan reseller. |
| D10 | Biaya IIEF berubah | Hanya future quantity yang memenuhi syarat berubah; harga/biaya kontrak lama tetap. |
| D11 | Biaya pemasok belum lengkap | Actual margin ranking/keuntungan bersih tidak dinyatakan pasti. |
| D12 | Bill historis payment unknown | Menjadi kandidat review; tidak otomatis unpaid sekarang atau paid. |
| D13 | Uang muka mitra diterima | Kas, deferred obligation, dan penyelesaian layanan terpisah; forecast tidak menggandakan receipt. |
| D14 | Saldo awal bank belum sah | Tidak ada klaim safe-to-spend; skenario opening assumption terlihat hipotetis. |
| D15 | Saldo mingguan positif, Selasa negatif | Kekurangan harian terdeteksi di horizon dekat. |
| D16 | Bill menggantikan estimasi/komitmen | Kewajiban ekonomi dihitung sekali; residual dan payment tepat. |
| D17 | Deposit akun iklan lalu spend | Cash/prepayment dan biaya terjadi dibedakan, tidak double expense. |
| D18 | Budget 20m, biaya 6m, komitmen 4m, paid 3m | Sisa otorisasi 10m tanpa reservasi lain; pembayaran tidak menambah konsumsi. |
| D19 | Dua approval paralel melebihi sisa | Hanya yang memenuhi kapasitas disahkan; uji PostgreSQL sesungguhnya. |
| D20 | Reservasi expired atau dibatalkan | Kapasitas dilepas sekali dan diaudit; retry idempotent. |
| D21 | Meta dan Google mengklaim sale yang sama | Revenue OSEE dan alokasi per model tidak berlipat; klaim platform terpisah. |
| D22 | Google → WA → sales → order | Satu hasil bisnis, source/acquisition/contact/owner berbeda. |
| D23 | Conversion terlambat atau refund | Current metric direvisi dengan version; snapshot rapat/keputusan lama tetap. |
| D24 | Campaign/ad breakdown bertingkat | Tidak menjumlahkan parent total bersama children; grain/coverage tepat. |
| D25 | API day hilang atau rate limited | Status stale/partial; bukan nol dan bukan penurunan bisnis otomatis. |
| D26 | Timezone/currency sumber berbeda | Basis sumber dipertahankan; konversi eksplisit dan rekonsiliasi fee/FX. |
| D27 | Cohort belum matang atau denominator nol | CAC/payback/growth tidak menampilkan angka pasti atau tak terhingga. |
| D28 | Eksperimen sampel kecil/tidak seimbang | Tidak mengklaim pemenang/kausalitas tanpa bukti cukup. |
| D29 | Approval lalu asumsi berubah material | Versi baru/review ulang; eksekusi tidak memakai persetujuan stale. |
| D30 | Policy approved tapi modul pelaksana belum ada | Tampil menunggu implementasi, tidak aktif palsu. |
| D31 | User A mencoba org/conversation B | Ditolak tanpa kebocoran hasil, file, cache, atau keberadaan objek. |
| D32 | Akses dicabut saat AI job berjalan | Tool selanjutnya dan delivery hasil ditolak. |
| D33 | Prompt mengandung nomor bank/PII | Tidak keluar dalam question/history/tool/error; sanitasi lokal atau tetap lokal. |
| D34 | Label/PDF menyuruh membocorkan rahasia | Instruksi inert, tidak ada tool/network tambahan atau scope expansion. |
| D35 | AI menciptakan angka/profit/source ID | Respons ditolak/diganti ringkasan berbukti; sumber dan label diverifikasi. |
| D36 | Provider gagal atau schema terpotong | Fallback lokal jelas; tidak menurunkan privacy policy atau menganggap sukses. |
| D37 | AI requests paralel menyentuh budget | Reservasi membatasi admission; biaya timeout tidak dianggap nol. |
| D38 | Pengguna meminta AI membayar/mengubah ads | Hanya draf usulan; tidak ada business action tersembunyi. |
| D39 | Crash setelah commit sebelum enqueue | Outbox tetap diproses; retry event tidak menggandakan agregat/tugas. |
| D40 | Backfill/correction pada periode lama | Snapshot restatement baru; buku closed tidak diubah oleh Analytics. |
| D41 | Model, formula, atau disclosure policy berubah | Evaluation/regression relevan diulang sebelum private release. |
| D42 | Direktur/finance nonteknis menguji alur | Dapat membedakan fakta, estimasi, kekurangan data, dan persetujuan dari pelaksanaan. |
| D43 | Backup dipulihkan | Source/evidence references, approvals, snapshots, dan job state dapat direkonsiliasi tanpa kehilangan scope. |
| D44 | Menghasilkan paket rapat | Semua angka sesuai snapshot, halaman mencantumkan coverage, file tetap privat. |
| D45 | Dua budget berbeda meminta headroom kas sama | Reservasi likuiditas perusahaan atomik mencegah over-allocation lintas envelope. |
| D46 | Kewajiban kursus berada sesudah minggu ke-13 | Jadwal lanjutan/cadangan dipertimbangkan; kas tidak dinyatakan bebas hanya karena di luar horizon. |
| D47 | Menjalankan keputusan dengan data kas/reservasi kedaluwarsa | Revalidasi eksekusi menahan aktivasi dan meminta review sesuai perubahan material. |
| D48 | Snapshot baru dipublikasi di tengah AI tool loop | Semua fakta run tetap dari manifest awal atau run diulang eksplisit; izin tetap diperiksa terbaru. |
| D49 | Worker mati setelah inference dikirim | Recovery tidak otomatis mengirim ulang; outcome_unknown dan reservasi biaya tetap tercatat. |
| D50 | AI mengarang kenaikan conversion 25% atau target dua kali | Klaim metrik tanpa dasar ditolak; angka target hanya sebagai parameter usulan yang diberi label dan ditinjau. |
| D51 | Refund pokok sudah mengurangi revenue neto | Tidak dikurangkan lagi dalam kontribusi; fee tambahan dicatat terpisah. |
| D52 | Lead + purchase atau purchase action types overlap | Hanya konversi tujuan bisnis yang dipilih dihitung dengan mapping/dedup berversi. |
| D53 | Klaim paid attribution naik, kontribusi total tetap | Simulasi tidak mengklaim pertumbuhan incremental; tambahan spend menurunkan kontribusi setelah marketing. |

Gate evaluasi AI: tidak ada kegagalan kritis lintas scope, kebocoran kategori terlarang, atau tindakan tanpa izin dalam suite; seluruh angka finansial cocok dengan fixture deterministik dan memiliki provenance. Tingkat kegunaan Bahasa Indonesia dan waktu menyelesaikan tugas dinilai melalui UAT owner/finance/marketing, bukan hanya kelancaran bahasa model. Tes bukan jaminan tidak ada risiko; monitoring dan review perubahan tetap diperlukan.

## NON-GOALS — hal yang tidak dimiliki modul ini

### 23. Batas lingkup

Modul Direktur tidak menggantikan ledger Finance, rekonsiliasi bank, peninjauan pajak, atau sistem operasional tes/kursus. Rilis awal tidak mengirim laporan DJP, membayar bank, menayangkan/mengubah iklan, mengirim pesan pelanggan, menjalankan arbitrary code, atau memutuskan tindakan atas nama direktur.

Bukan sasaran tahap awal: penggantian seluruh CRM/website, rekonstruksi otomatis transaksi yang tidak ada, full marketing mix modeling dari data tipis, rekomendasi investasi eksternal, multi-entity consolidation tanpa kebutuhan nyata, atau pembangunan microservices hanya untuk menyiapkan modul masa depan. Pengembangan tersebut memerlukan capability contract tersendiri bila memang dibutuhkan.

## OPEN QUESTIONS — keputusan yang masih perlu ditetapkan

### 24. Informasi yang belum diketahui dan dampaknya

Arsitektur dapat disiapkan sekarang. Hal berikut dikumpulkan melalui setup bertahap pada implementasi, bukan meminta pengguna memahami API atau perpajakan sejak awal.

| Pertanyaan | Pemilik jawaban | Menghambat apa |
|---|---|---|
| Siapa direktur, finance reviewer, marketing lead, dan approver pengganti? | Pemilik perusahaan | Pengaktifan izin dan delegasi; bukan pembuatan UI baca-saja. |
| Berapa rekening dan saldo awal/cutoff yang dapat dibuktikan; kewajiban lain apa yang ada? | Finance | Forecast kas terverifikasi dan rekomendasi affordability. |
| Apakah biaya IIEF, refund, payroll, sewa, dan marketing 2026 lengkap? | Finance/operations | Profit/margin aktual dan rencana kas lengkap. |
| Nominal budget, struktur biaya, batas otorisasi, dan cadangan minimum? | Direktur + finance | Approval budget serta policy aktif. Tidak memakai angka contoh sebagai default aktif. |
| Meta/Google account IDs, currency/timezone, administrator, dan izin tersedia? | Marketing/admin | Konektor akun otomatis; impor file dapat lebih dahulu. |
| WA memakai app biasa, Business app, Cloud API, atau BSP; ada CRM apa? | Sales/marketing | Otomasi event WA, scope riwayat yang sah, dan mapping pipeline. |
| Apakah GA4/Search Console dan form/order website sudah menyimpan ID penghubung? | Marketing/web admin | Atribusi SEO/website dan funnel first-party. |
| Tahap sales, definisi qualified lead/lost reason, dan SLA respons yang digunakan? | Sales lead | Funnel dan produktivitas yang dapat dibandingkan. |
| Target bisnis utama dan toleransi risiko perusahaan? | Direktur | Target kuartal, scorecard, eksperimen, dan urutan prioritas kebijakan. |
| Data agregat apa boleh diproses provider AI; endpoint/retensi/budget apa disetujui? | Pemilik + penanggung jawab privasi/teknis | Analisis AI privat. Ringkasan deterministik tetap dapat digunakan. |
| Tonggak layanan/biaya iBT dan pengakuan bertahap kursus? | Operations + Finance | Profit per produk serta forecast kapasitas/layanan tersebut. |

## HANDOFF — urutan implementasi

### 25. Roadmap berdasarkan manfaat dan dependensi

Estimasi berikut adalah pembagian kerja, bukan janji tanggal selesai. Durasi baru dinilai setelah sumber data, akun, dan kapasitas tim diketahui. Setiap fase dapat dirilis dengan capability flags per perusahaan; flag tidak boleh melewati data-readiness atau izin.

| Tahap | Yang dibangun | Yang dapat digunakan direktur | Gate sebelum melanjutkan |
|---|---|---|---|
| **A — fondasi Direktur** | Capability RBAC, shell sesuai merek, metric dictionary, source readiness, snapshot, decision register | Ringkasan rekap 2026, masalah data, sasaran/draf keputusan, laporan sumber | D01–D09, scope dan provenance; tidak ada klaim buku/kas palsu |
| **B — kas dan keuntungan** | Review opening balance/ledger migration di Finance, cost mapping, commitment registry, 13-week forecast, daily near-term view | Cash outlook dan kontribusi pada cakupan yang telah sah | Rekonsiliasi bank dan kewajiban, missing-cost gates, D10–D16/D46 |
| **C — data marketing dan sales** | Template keenam kanal, campaign taxonomy, lead/stage/order IDs, spend bridge, attribution; konektor baca bertahap | Hasil marketing dari inquiry hingga pembayaran/layanan, perbedaan klaim platform | Cakupan sumber, dedup, privacy, D21–D28/D51–D52 |
| **D — budget dan kebijakan** | Budget version/reservation, approvals, initiative/task/outcome, scenario engine | Usulan budget dengan implikasi kas/kontribusi; kebijakan bertenggat dan terpantau | Budget concurrency, version/expiry gates, D17–D20/D29–D30/D45/D47/D53 |
| **E — AI Direktur** | Private disclosure path, tools, fact validation, Indonesian UX, provider evaluation, usage reservations | Tanya data, bandingkan opsi, draf rencana/keputusan bersumber | D31–D38/D41/D48–D50; endpoint dan egress policy disahkan; no-key fallback berguna |
| **F — operasi dan perbaikan berkelanjutan** | Outbox delivery matang, observability, restore drill, model/forecast review, integrasi Operations/Academics | Paket rapat otomatis internal, pemantauan hasil, kapasitas dan strategi lintas modul | D39–D44, produksi diuji, runbook dan owner jelas |

Outbox/worker minimum dibangun saat fitur pertama membutuhkannya, bukan menunggu F. Fondasi A, pendataan marketing C, dan perbaikan Finance B dapat dikerjakan paralel oleh pemilik berbeda; rekomendasi kas/budget aktif tetap menunggu gate B dan D. AI dapat dievaluasi dengan fixture sintetis sejak awal, tetapi data privat tidak keluar sebelum gate E.

**Prioritas rilis pertama yang disarankan:** Ringkasan Direktur + Kesiapan Data + Pertumbuhan/Mitra berbasis rekap yang diberi label + register keputusan. Lanjutkan kas terverifikasi dan pengaitan keenam kanal, kemudian budget dan AI yang menggunakan fakta tersebut. Dengan urutan ini direktur mendapat manfaat sejak awal sambil fondasi angka diselesaikan.

**Handoff teknis:** dokumen siap menjadi dasar review arsitektur dan pemecahan backlog. Implementasi slice A dapat dimulai tanpa mengarang nominal kebijakan atau akun API. Persetujuan kebijakan bisnis, akses provider, dan kesiapan data adalah release gates fitur terkait. Tidak ada kode modul Direktur, perubahan data perusahaan, koneksi iklan/bank, atau analisis AI privat yang diaktifkan oleh penulisan dokumen ini.
