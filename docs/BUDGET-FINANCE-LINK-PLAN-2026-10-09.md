# Komitmen budget, realisasi parsial dan tagihan Finance

Status: DIR-02/DIR-03 diterima untuk cakupan realisasi parsial dan hubungan eksplisit tagihan/pembayaran Finance pada source `3bb3bda4d34e11760d3c609e7ca373a4d4cbba24a4d0c3ee11e4c2232277b2d5`. Gate `budget-final-2` menghasilkan `READY_FOR_SCOPE`; skema lokal sudah diterapkan, 11 pemeriksaan HTTP lulus dan data lama tetap utuh setelah startup. Aplikasi lokal berjalan di `http://127.0.0.1:8001`. Lihat [laporan penerimaan dan batasnya](BUDGET-FINANCE-LINK-RELEASE-2026-10-09.md). Persetujuan budget tidak dianggap sebagai transfer uang atau pengakuan biaya.

## Hasil bagi staf

Direktur melihat satu baris belanja dengan tujuan, nilai disetujui, biaya yang sudah terjadi, komitmen tersisa, uang yang dibayar dan bukti tagihan. Finance dapat menghubungkan tagihan yang sudah ada dan mencatat realisasi sebagian. Pembayaran tidak menghabiskan budget untuk kedua kalinya. Satu angka yang sama digunakan pada halaman, laporan dan konteks AI.

Contoh sintetis: komitmen Rp80 juta, pembayaran Rp30 juta dan realisasi Rp20 juta menghasilkan biaya terjadi Rp20 juta, komitmen tersisa Rp60 juta dan konsumsi budget Rp80 juta. Pembayaran Rp30 juta tetap angka arus kas terpisah.

## Mengapa perubahan dibutuhkan

Sebelum batch ini, `BudgetEntry` mempunyai jenis incurred/commitment/reservation/payment, penarikan satu baris dan pengganti seluruh baris. Pembayaran dengan hubungan `settles` menjaga sebagian risiko hitung ganda, tetapi hubungan pembayaran membatasi penggantian komitmen dan tidak menyediakan perpindahan parsial atau residual. Hubungan bertipe ke Bill, jurnal atau bank belum tersedia. `Budget.version` mencatat siklus envelope; perubahan baris belum merupakan versi posisi belanja yang lengkap. AI menghitung sebagian data baris sendiri. Celah tersebut kini ditangani oleh register dan posisi kanonis di bawah; riwayat lama dipertahankan.

Finance sudah mempunyai Bill yang disetujui, PREPAID/EXPENSE, pelepasan prepayment, pembayaran Bill biasa parsial serta pelunasan pemasok dengan potongan penuh yang didukung. Tidak ada worker DomainEvent lengkap untuk memproyeksikan seluruh kejadian tersebut. Adapter sinkron dalam transaksi yang sama lebih kecil dan dapat dipulihkan secara atomik.

## Register dan antarmuka

| Register | Fakta yang disimpan |
|---|---|
| BudgetSpend | Identitas belanja stabil, budget disetujui, baris asal, tujuan/sumber awal, versi operasional |
| BudgetSpendMovement | Perpindahan parsial reservation→commitment→incurred, pelepasan residual, tanggal aktual, actor, alasan/bukti, referensi retry; immutable |
| BudgetBillLink | Seluruh Bill yang dipilih secara eksplisit, identitas/fingerprint sumber dan coverage terhadap estimasi/realisasi manual; satu Bill hanya pada satu belanja |
| BudgetPaymentMatch | Hubungan bertipe antara pembayaran manual dan fakta pembayaran Finance yang sama, sehingga dicatat sekali; sumber manual tetap tersimpan |

Pertahankan `BudgetEntry`, `settles`, pembayaran dan jurnal lama. Jangan mengubah riwayat Finance melalui koreksi Direktur. Satu belanja dapat memiliki beberapa Bill, tetapi alokasi satu Bill ke beberapa belanja memerlukan kontrak tersendiri dan ditolak pada batch awal.

Antarmuka `budget_position(*, organization, budget, as_of)` mengembalikan incurred, remaining commitment/reservation, available authorization, overrun, reported payments, verified cash outflow, remaining Bill liability, remaining withholding liability, references, issues dan fingerprint. Halaman, ringkasan/PDF/CSV/API manajemen dan konteks AI kini memakai posisi kanonis; properti model tidak membuat kalkulasi alternatif. Kalkulator lama yang tidak dipakai pembaca produksi tetap tersedia, tetapi bukan sumber angka persisten. Integrasi konsumen berada di `director/budget_reporting.py` dan diuji terhadap posisi sumber yang sama.

Setelah baris lama diadopsi ke Spend, jalur add/payment/void/replace lama harus menolak perubahan langsung atau mengarahkannya ke mutasi Spend yang sama. Riwayat `settles` dan penggantian tetap utuh; satu baris tidak boleh ikut dihitung sebagai sumber lama dan sumber Spend sekaligus.

Mutasi publik: adopsi baris lama sebagai belanja; konversi sebagian; hubungkan Bill dengan coverage terpilih; lepaskan residual; cocokkan pembayaran manual dengan pembayaran Finance. Setiap permintaan membawa versi/fingerprint yang dilihat staf dan referensi unik. Retry identik mengembalikan catatan lama; isi berbeda dengan referensi yang sama ditolak.

Modul `director.spend_services` menyediakan `budget_position`, `spend_position`, `adopt_budget_entry`, `convert_spend`, `link_spend_bill`, `release_spend_residual`, `record_spend_payment` dan `match_spend_payment`. Koreksi Owner memakai `correct_spend_movement`, void/correct link Bill dan void/correct payment match dengan reversal/riwayat baru. Salah nominal, salah tagihan atau salah pasangan pembayaran harus dapat dipulihkan tanpa mengedit fakta Finance. Sumber yang sudah dipakai tidak boleh dilepas melalui koreksi kapasitas yang bertentangan.

Posisi kanonis mengembalikan amount, incurred, open_commitments, reservations, consumed, available, overrun, payments, reported_payments, unmatched_reported_payments, verified_cash, unpaid_bills, withholding_payable, spends, references, issues, current, fingerprint, as_of dan version. Nominal berupa string desimal kanonis; sumber korup/incomplete menghasilkan `current=false`, tugas pemeriksaan dan total finansial belum dapat dinilai. Nilai kosong tidak menjadi nol. Pembayaran gabungan menghitung kas terverifikasi ditambah pengamatan manual yang belum cocok, sekali per kejadian; komponen keduanya tetap terlihat.

Adapter `finance.budget_adapter.bill_budget_facts` memvalidasi jurnal/bank asli dan menyediakan gross, incurred, commitment, AP settled/outstanding, withholding created/remitted/outstanding, verified cash, event bertipe serta fingerprint. Lima hook Finance memanggil `sync_bill_budget` setelah kejadian lengkap dalam transaksi yang sama. Persetujuan/pembayaran/release asli tidak dibuat ulang saat link backfill. Sumber tanpa link tetap jalur lama; sumber ber-link memerlukan actor aktif yang nyata, tanpa menciptakan atribusi Owner palsu.

## Kontrak perhitungan

Konsumsi otorisasi = biaya terjadi + komitmen tersisa + reservation tersisa. Perpindahan bucket tidak menambah konsumsi; pembayaran tidak mengubahnya.

Bill prepaid mempertahankan jumlah belum diakui sebagai komitmen. `PrepaymentRelease` memindahkan nominal yang diakui menjadi incurred. PPN yang sudah berada dalam bruto tagihan dihitung sekali. Pelunasan pemasok dengan potongan membersihkan kewajiban bruto; uang keluar tetap pembayaran neto pemasok ditambah penyetoran potongan yang benar-benar terjadi. Jangan menghitung keduanya sebagai biaya baru.

Coverage eksplisit menggantikan estimasi/realisasi yang sudah mewakili Bill. Menghubungkan Bill ke komitmen tidak boleh menambahkan seluruh bruto di samping komitmen tersebut. Sumber lama yang tidak cocok tidak dihapus untuk membuat angka tampak benar.

Pembayaran manual yang belum cocok diberi label “dilaporkan”; angka kas terverifikasi berasal dari fakta bank Finance. Pencocokan terhadap kejadian yang sama menghitung sekali. Pelepasan nilai yang belum dipakai memerlukan alasan/bukti dan tidak dapat menghapus kewajiban Bill terposting, biaya terjadi atau nominal bersengketa yang sudah diselesaikan.

Biaya/tagihan terposting yang melebihi estimasi tetap masuk posisi aktual dan menampilkan kelebihan budget serta tugas pemeriksaan Owner. Jangan menyembunyikan fakta Finance atau menolak hubungan hanya agar konsumsi terlihat di bawah batas. Kenaikan envelope memerlukan perubahan otorisasi tersendiri dengan alasan dan persetujuan; adapter tidak membuat tambahan budget otomatis. `CostAllocation` hanya atribusi biaya dan tidak menambah incurred di samping Bill yang sama.

Bill draft yang dihubungkan tetap berstatus rencana sampai jurnal approval terposting. Bill dan BillPayment belum mempunyai FK jurnal: resolve source key tepat `bill:<id>:approval` dan `bill-payment:<id>`, lalu periksa tanggal, organisasi seluruh baris, akun, status dan jumlah. Event release/withholding/remittance memeriksa FK jurnal/bank yang bertipe beserta rincian sumbernya. Fingerprint berubah ketika identitas sumber berubah walaupun nominal sama.

Pemilihan pembayaran sampai cutoff menggunakan tanggal bank atau jurnal tepat dalam organisasi. Jika tanggal bank berubah melewati cutoff, jurnal asli sebelum cutoff tetap membuat sumber diperiksa dan ketidakcocokan tanggal menolak bukti; pembayaran historis tidak diam-diam menjadi nol. Pembayaran yang bank dan jurnalnya sama-sama sesudah cutoff tetap dikecualikan.

Sisa AP pemasok berbeda dari sisa potongan `TAX_PAYABLE`. Pelunasan bruto Bill tidak berarti potongan sudah disetor. Tampilkan keduanya terpisah; kas terverifikasi hanya neto pemasok dan penyetoran nyata, termasuk penyetoran sebagian yang didukung. Pencocokan pengamatan manual terhadap pembayaran Finance memiliki coverage terbatas jumlah dan tidak dapat digunakan lagi melebihi sumbernya.

Tanggal Finance aktual tidak diubah menjadi bulan budget. Tampilkan periode otorisasi dan bulan akuntansi secara terpisah. Sumber tidak lengkap/tidak cocok menghasilkan tugas pemeriksaan dan nilai belum dapat dinilai, bukan angka nol atau fallback diam-diam.

Nilai cakupan hubungan tidak boleh melebihi bruto Bill terkini. Jika Bill draft dikurangi setelah dihubungkan, fakta Finance yang sah tetap tercatat; posisi budget menjadi belum dapat dinilai dan meminta koreksi hubungan oleh Owner. Selisih estimasi tidak dilepas otomatis. JSON sumber yang rusak menghasilkan tugas pemeriksaan, bukan kegagalan halaman.

Batas otorisasi berasal dari versi budget yang saat ini disetujui. Tanggal posisi menunjukkan cakupan kejadian belanja dan pembayaran; model ini belum merekonstruksi kapan persetujuan otorisasi historis mulai berlaku. Revisi otorisasi dan paket rapat permanen tetap pekerjaan DIR-06/DIR-07.

## Peran, konsistensi dan pemulihan

Finance menyiapkan hubungan dan mencatat realisasi; Owner menyetujui perubahan otorisasi serta pelepasan/koreksi. Marketing tidak dapat mengubah konsumsi keuangan. Batas organisasi, actor aktif, CSRF dan versi berlaku pada server, bukan hanya tombol.

Semua mutasi memperoleh kunci Organization terlebih dahulu untuk serialisasi organisasi, lalu sumber Finance dan Budget/Spend dalam urutan yang dinyatakan masing-masing antarmuka. Hook Finance yang sudah mengunci Bill/obligation tetap mengikuti Org-first; jangan mengaku Budget selalu dikunci sebelum Bill jika adapter dipanggil setelahnya. Tidak ada jalur baru yang memperoleh Organization setelah kunci anak. Hook persetujuan Bill, pelepasan prepayment, pembayaran, pelunasan potongan dan remittance menjalankan adapter dalam transaksi yang sama. Kegagalan integritas adapter membatalkan posting beserta pembaruan budget; overrun menghasilkan tugas pemeriksaan, bukan penolakan fakta Finance yang sah. Menghubungkan Bill yang sudah disetujui atau dibayar sebagian membaca fakta lama dan tidak membuat jurnal bank baru.

Riwayat belanja menyimpan sebelum/sesudah dan identitas sumber. Koreksi membuat versi/pengganti; tidak mengedit jurnal terposting. Ringkasan lama dan persetujuan AI perlu fingerprint yang cocok sebelum dipakai kembali.

## Kepemilikan implementasi

| Area | Berkas |
|---|---|
| Backend Direktur | `director/spend_models.py`, `spend_services.py`, `tests_spend.py`, `tests_spend_concurrency.py`, migrasi; import models dan guard jalur lama di models/services |
| Konsumen angka | `director/calculators.py`, properti Budget, `views.py`, `analytics.py:dashboard_snapshot`, `documents.py:management_report_pdf`, export/API dan `ai.py:_planning_facts/source_references`; semua memakai posisi/fingerprint yang sama |
| Adapter Finance | `finance/budget_adapter.py`, `tests_budget_link.py`; hook terbatas pada approval/release/payment/withholding/remittance |
| UI | Form/rute/halaman budget Direktur dan tagihan Finance; uji HTTP dan browser |
| Pemeriksaan independen | Alur lintas modul, source integrity, rollback, cutoff, retry dan race PostgreSQL nyata |

## Penerimaan minimum

- Komitmen80/pembayaran30/realisasi20 → incurred20, komitmen60, konsumsi80, pembayaran30; settlement lama tetap.
- Reservation80/Bill prepaid60/release20 → reservation20, komitmen40, incurred20, konsumsi80.
- Pembayaran biasa20 lalu15 → kas terverifikasi35 tanpa tambahan konsumsi.
- Pengamatan pembayaran manual yang dicocokkan dengan alokasi bank sama dihitung sekali.
- Hubungkan Bill yang sudah disetujui/dibayar sebagian tanpa jurnal atau alokasi baru.
- Biaya aktual di atas estimasi terlihat sebagai overrun dengan tugas Owner; tidak ada biaya atau envelope yang disembunyikan/ditambah otomatis.
- Pelunasan neto pemasok lalu remittance sebagian menjaga AP dan utang potongan terpisah; atribusi CostAllocation tidak menggandakan biaya.
- Retry identik, konflik isi, versi/fingerprint stale, organisasi lain, actor tidak aktif dan Marketing ditangani tanpa mutasi tambahan.
- Dua konversi/reservation bersamaan tidak memakai kapasitas yang sama; dua belanja tidak menghubungkan satu Bill bersamaan.
- Kegagalan setelah posting membatalkan perubahan Finance dan budget bersama-sama; pelepasan/koreksi tidak sah menjaga riwayat.
- Tanggal akuntansi lintas bulan tetap asli. Sumber hilang/rusak terlihat sebagai blocker.
- Kejadian bertanggal di masa depan tidak masuk total cutoff sebelumnya. Mengadopsi catatan lama tidak menghapus nilai historis asal; koreksi mempertahankan tanggal asli dan tanggal koreksi nyata.
- Salah konversi, hubungan Bill atau pasangan pembayaran dapat diperbaiki Owner melalui riwayat baru; stale/konflik kapasitas ditolak tanpa mengubah Finance.
- Skenario browser desktop/ponsel memverifikasi data tersimpan serta tugas yang jelas, bukan hanya tampilan sukses.

Supplier advance/refund, split-Bill, otorisasi likuiditas perusahaan dan pemotongan pemasok parsial tetap kontrak terpisah. QA sintetis tidak menggantikan sumber nyata atau UAT staf.

## Hasil penerimaan cakupan

Pengujian final pada source yang sama: PostgreSQL17 876 lulus tanpa skip; SQLite 876 dijalankan dengan 839 lulus dan 37 kasus khusus PostgreSQL dilewati; 12 kasus independen lulus; browser 10 alur/132 keadaan desktop dan ponsel. Gate mencatat 13 temuan produk terselesaikan tanpa blocker cakupan. [Laporan rilis](BUDGET-FINANCE-LINK-RELEASE-2026-10-09.md) menyimpan referensi bukti, percobaan gagal, preservasi lokal, status runtime dan batas penerimaan. [MKT-03](MARKETING-AD-RECOVERY-PLAN-2026-10-09.md) menjadi kontrak batch berikutnya; seluruh backlog ERP tetap terbuka.
