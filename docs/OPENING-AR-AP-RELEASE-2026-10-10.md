# FIN04 — piutang dan utang lama: penerimaan alur terbatas

**Status: `READY_FOR_SCOPE`, `company_installation: installed_local`.** Alur FIN04 yang terbatas sudah diverifikasi dan terpasang pada aplikasi lokal perusahaan. Penerimaan ini mencakup pokok piutang/utang lama, kas nyata dan ketergantungan pemeriksaan fiskal terpisah. Dokumen ini belum menjadi pernyataan seluruh ERP siap dipakai. `all_erp_ready: false`.

Source yang diterima dan terpasang, freeze 4: `bf4a8d16ce3ecf07fb406786308a3735f9ca2feb7bc8e0d9b7902d26a438f9c7`. Identitas source sama pada worktree pengembangan dan PRIMARY. Dokumen ini membedakan bukti pada source tersebut dari hasil antara pada source sebelumnya.

Aplikasi perusahaan di PRIMARY `D:\ERP osee` sekarang memakai FIN04 pada **http://127.0.0.1:8001/**. Staf membuka **Finance → Piutang & utang lama** setelah masuk. Backup, preservasi data dan pemeriksaan runtime pemasangan telah lulus. Pengujian fitur memakai data sintetis terpisah; pemeriksaan pemasangan perusahaan tidak melakukan login, menambah transaksi bisnis atau memanggil provider.

## Apa yang tersedia

Staf membuka **Finance → Piutang & utang lama**. Owner/Direktur mencatat sisa piutang pelanggan/mitra atau utang pemasok yang benar-benar ada ketika pembukuan mulai digunakan. Finance/Owner kemudian mencocokkan penerimaan atau pembayaran terhadap mutasi bank asli.

| Tindakan | Pengguna | Isian dan hasil |
|---|---|---|
| Tambah saldo lama | Owner/Direktur aktif | Jenis piutang/utang, pihak, tanggal saldo awal, tanggal dan nomor dokumen asal, sisa pokok, alasan dan bukti. |
| Cocokkan uang bank | Finance/Owner aktif | Pilih mutasi bank nyata, isi nominal pokok, periksa sisa, lalu konfirmasi. Tanggal mengikuti bank. |
| Baca posisi dan riwayat | Akun dengan akses keuangan yang sesuai | Posisi pada tanggal yang dipilih dan catatan sumber; pembacaan tidak membuat transaksi. Auditor membaca, Marketing tidak mendapat akses. |
| Siapkan pemeriksaan kas | Finance/Owner aktif | Cocokkan PDF laporan profesional dan fakta manusia melalui formulir. Finance dapat menyiapkan/periksa ulang; Owner mencatat kesimpulan yang diterima. |

Formulir pencatatan/pencocokan memakai tiga langkah: **pilih dan isi → periksa sumber → konfirmasi dan simpan**. Sumber yang berubah ditolak saat penyimpanan; isian tetap tersedia. Pemeriksaan ulang memperbarui identitas sumber dan menghapus konfirmasi agar staf memeriksa keadaan terbaru.

Seluruh item perusahaan menggunakan satu tanggal awal. Tanggal dokumen asal tidak boleh melewati tanggal tersebut, dan tanggal awal tidak boleh sesudah jurnal terposting paling awal. Dokumen yang sudah menjadi invoice/tagihan di aplikasi tidak dapat dibawa lagi sebagai saldo lama. Perlindungan ini juga berlaku saat dokumen biasa dibuat setelah saldo lama tercatat.

## Pembukuan dan posisi bertanggal

| Kejadian | Debit | Kredit |
|---|---|---|
| Saldo piutang lama | Piutang | Saldo awal/ekuitas |
| Saldo utang lama | Saldo awal/ekuitas | Utang |
| Penerimaan pokok nyata | Bank | Piutang |
| Pembayaran pokok nyata | Utang | Bank |

Pencatatan awal tidak membuat invoice, tagihan, mutasi bank, layanan, penjualan atau biaya periode berjalan. Pelunasan mengurangi pokok dan menggunakan uang bank yang benar-benar tersedia. Satu mutasi memakai kapasitas pencocokan yang sama dengan alur keuangan lain; tidak memperoleh kapasitas kedua.

Posisi historis tidak meminjam pembayaran yang terjadi setelah tanggal laporan. Pembayaran sebagian atau seluruhnya dibatasi sisa pokok pada riwayat dan kapasitas mutasi bank. Permintaan ulang dengan identitas dan fakta persis sama mengembalikan catatan semula tanpa jurnal atau kas tambahan.

Rincian piutang/utang lama menjadi bagian dari total neraca dan persamaan subledger penutupan, bukan tambahan yang dijumlah dua kali. Sumber rusak menghasilkan status perlu pemeriksaan dan nominal tidak dapat dinilai; tidak diganti dengan saldo nol.

## Kas dan pajak diperiksa terpisah

Kas nyata dapat dicatat sementara pemeriksaan pajaknya masih terbuka. Nominal bank, tanggal saldo awal, atau tanggal dokumen lama tidak menentukan bruto, potongan, kredit pajak, maupun masa penghasilan secara otomatis.

Setiap penerimaan/pembayaran memiliki pemeriksaan profesional atas **peristiwa kas asli**. Laporan terikat pada settlement, dokumen pokok, identitas legal pihak, sumber bank/jurnal, PDF yang benar-benar diterima, serta riwayat versi. Formulir tidak meminta staf mengetik JSON.

Cabang yang didukung untuk melepas pemeriksaan kas adalah laporan profesional yang secara jelas menyimpulkan **tidak ada pemotongan, dengan nominal `0.00` eksplisit**. Kolom kosong berarti belum ditentukan. Nominal positif, permintaan bukti tambahan, atau perlakuan yang belum didukung tetap ditahan; tidak dibuat menjadi nol, kewajiban atau kredit fiktif.

Tugas tetap tampil saat sisa pokok sudah lunas. Perubahan pihak, sumber atau laporan membuat pemeriksaan perlu diulang. Revisi yang bergantung pada buku tertutup, persetujuan bulanan, kewajiban yang masih berlanjut, atau paket tahunan disetujui/dilaporkan ditahan sampai alur koreksi terkait diselesaikan. Dokumen dan keputusan lama tetap tersimpan.

Penerimaan piutang lama pada bulan terjadinya menjadi kandidat fiskal tersendiri: kas aktual, bruto belum ditentukan, dan tanpa invoice fiktif. **Tidak ada pilihan otomatis untuk memasukkannya sebagai omzet atau mengecualikannya hanya karena dokumennya lama.** Laporan profesional bulanan menentukan masuk, dikeluarkan, atau penyesuaian waktu berdasarkan bukti. Keputusan bulanan ini terpisah dari pemeriksaan pemotongan atas kas asli. Pembayaran utang lama tidak menjadi kandidat omzet.

Kas bulan terdahulu tetap menjadi ketergantungan pemeriksaan, termasuk ketika pokok sudah nol, tetapi tidak diulang sebagai kandidat penerimaan bulan berikutnya. Perubahan keputusan profesional mengubah identitas paket fiskal dan dapat membuat persetujuan lama tidak mutakhir; hash sumber komersial tidak memasukkan kepala keputusan fiskal. Sumber pokok tanpa pembayaran juga terikat pada snapshot tahunan agar kerusakan bukti tidak hilang dari pemeriksaan.

CSV draf menyediakan baris pemeriksaan kas saldo lama secara terpisah. Bruto, dasar, tarif dan nominal pajak yang belum ditentukan tetap kosong. CSV/paket pemeriksaan ini belum menjadi SPT, XML DJP, bukti bayar, atau bukti pelaporan resmi.

## Bukti verifikasi yang sudah tersedia

| Pemeriksaan | Hasil pada candidate freeze 4 | Batas bukti |
|---|---|---|
| Targeted SQLite, attempt 4 | 94 kasus: **87 lulus, 7 dilewati**, exit `0`; source sebelum/sesudah persis freeze 4; teardown normal. | Tujuh kasus kompetisi memerlukan PostgreSQL nyata; hasil SQLite tidak membuktikan row locking PostgreSQL. |
| SQLite lengkap, attempt 2 | 1.162 kasus: **1.101 lulus, 61 dilewati**, exit `0`; source sebelum/sesudah persis freeze 4; native `480.050` detik, teardown database uji normal. | Kasus yang dilewati tidak dihitung lulus; kompetisi targeted dibuktikan tersendiri pada PostgreSQL. |
| Targeted PostgreSQL, attempt 5 | **94 lulus, tanpa skip**, exit `0`; source sebelum/sesudah persis freeze 4; database uji dibongkar normal. Instance uji milik perjalanan dihentikan melalui smart stop normal dan bersih, tanpa pembersihan paksa. | Membuktikan kasus targeted, termasuk kompetisi nyata; bukan seluruh ERP atau deployment perusahaan. |
| Replay probe publik asli, attempt 4 | **2 lulus**, exit `0`; source persis freeze 4, teardown normal, byte probe asli tidak berubah. | Membuktikan kontrak publik yang diperiksa probe, bukan seluruh ERP. |
| Replay probe organisasi asli, attempt 1 | **1 lulus** dengan tiga assertion asli, exit `0`; source persis freeze 4, teardown normal, byte probe RED tidak berubah. | Membuktikan bahwa pokok asli yang dipindahkan tetap belum dapat dinilai, blocker tetap ada, dan penutupan publik menolak sumber rusak. |
| Quality, attempt 4 | Django check, `makemigrations --check --dry-run`, Ruff `E9,F63,F7,F82`, compile, dan `pip check` **lulus**, exit `0`; source sebelum/sesudah persis freeze 4. | Tidak membuktikan perjalanan browser, pemasangan, atau kelengkapan seluruh kemampuan. |
| Browser desktop/ponsel, attempt 2 | **8 alur lulus, 156 keadaan diamati**; tidak ada error skrip atau overflow horizontal. Finance ponsel berjalan dengan JavaScript dinonaktifkan. Empat alur pencatatan/pencocokan lulus baca ulang finansial native akhir atas sumber utama dan tambahan. Browser dan WSGI ditutup normal; source/helper/freeze tetap sama. | Data sintetis, bukan UAT pekerjaan staf atau koneksi provider perusahaan. |
| Penerimaan teknis lingkup FIN04 | Receipt `opening-acceptance-1.json`: **`accepted_for_scope`**, source freeze 4, 342 berkas bukti aktual diikat hash, `all_erp_ready: false`. | Receipt dibuat sebelum pemasangan dan tetap menyimpan `company_installation: pending` saat itu; status terpasang dibuktikan oleh receipt runtime berikutnya. |
| Pemasangan perusahaan | **Lulus, `installed_local`**; source PRIMARY persis freeze 4. Backup diverifikasi, dua migrasi tepat, seluruh 130 tabel/1.010 baris asli dipertahankan, nol baris bisnis ditambah. Sebelas pemeriksaan HTTP anonim/aset/health/ready/rute lulus sesudah restart normal. | Tidak melakukan login perusahaan, transaksi bisnis, koneksi provider atau UAT staf; cloud/domain/HTTPS belum dibuktikan oleh pemasangan lokal ini. |

Root juga memeriksa tampilan ponsel saat Owner mengisi/memeriksa saldo lama dan Finance mencocokkan/memeriksa kas, serta daftar Finance desktop. UAT oleh staf dengan dokumen serta pekerjaan nyata tetap terbuka.

## Pemasangan dan preservasi perusahaan

Handoff kedua selesai dengan exit `0`. Pemasangan menyalin 43 berkas source yang telah diterima dan menjalankan tepat dua migrasi: `finance.0014_opening_balances` serta `taxes.0012_opening_balance_reviews`. Tiga register bisnis baru tetap kosong. Lima belas definisi framework ditambah; **tidak ada baris bisnis perusahaan yang ditambah**. Nominal piutang/utang asli belum diimpor tanpa dokumen sumber yang lengkap.

Baseline aktual tepat sebelum pemasangan berisi **130 tabel dan 1.010 baris**. Seluruh tabel lama, kolom, nilai, identitas, sequence bisnis, skema historis, timestamp migrasi lama dan unggahan dipertahankan. Angka ini berasal dari baseline FIN04 aktual, bukan baseline pemasangan FIN03 yang lebih lama.

Backup baru `D:\ERP osee\.local\backups\before-opening-balances-20261009T215644Z.zip` dan backup source `before-opening-balances-source-20261009T215644Z.zip` diverifikasi terhadap isi serta hash. Perbandingan sesudah migrasi dan receipt runtime dengan stamp yang sama berstatus `pass`.

Restart normal mengaktifkan dotenv dan menggunakan database/media perusahaan yang sebenarnya. Pemeriksaan anonim membuktikan `/health/`, `/ready/`, `/login/` dan aset CSS memberi `200`; tujuh rute terlindungi memberi `302` menuju alur login. Receipt runtime mencatat total sebelas pemeriksaan berhasil serta `company_authentication_or_business_write_or_provider: false`. Pemeriksaan ini tidak menggantikan pengujian pekerjaan oleh staf.

Hasil antara berikut benar-benar terjadi pada source sebelumnya, `01d42f1551a3350c060ea98af206bc85cc4c36975ff5929687bc15779465a15f` (freeze 3). Source tersebut kemudian berubah karena perbaikan P2 ketiga. Hasilnya tetap disimpan, tetapi tidak dipindahkan menjadi klaim lulus untuk freeze 4.

| Pemeriksaan pada freeze 3 | Hasil aktual | Batas bukti |
|---|---|---|
| Targeted SQLite, attempt 3 | 91 kasus: **84 lulus, 7 dilewati**, exit `0`, source stabil. | Hasil sebelum tiga regresi organisasi ditambahkan. |
| SQLite lengkap, attempt 1 | 1.159 kasus: **1.098 lulus, 61 dilewati**, exit `0`, source stabil, teardown normal. | Regresi lengkap hanya untuk freeze 3. |
| Targeted PostgreSQL, attempt 4 | **91 lulus**, tidak ada skip, exit `0`, source stabil, teardown normal. | Termasuk kompetisi nyata pada freeze 3; bukan penerimaan freeze 4. |
| Replay probe publik asli, attempt 3 | **2 lulus**, exit `0`; byte probe tidak berubah. | Membuktikan kontrak publik yang diperiksa probe, bukan seluruh ERP. |
| Quality, attempt 3 | Django check, `makemigrations --check --dry-run`, Ruff `E9,F63,F7,F82`, compile, dan `pip check` lulus. | Hasil hanya pada source freeze 3. |
| Browser, attempt 1 | **Gagal**, exit `1`: assertion alat privat membandingkan string saldo bank `1000` dengan `1000.00`. Browser dan server milik perjalanan dihentikan normal; source tetap stabil. | Perjalanan tidak selesai. Kesetaraan nilai desimal tidak mengubah hasil gagal menjadi lulus; desktop/ponsel dan alur berikutnya belum dibuktikan. |

Identitas probe publik asli: `4d981416a888b63c85249c5e42d0a0dfbc7013a63e8aaa043f2e843bbdb545ad`.

Identitas probe organisasi asli: `efcf45772c49f58d5cbb78e673c2b9a1d27a59235de052eb3df717548be7eaf6`.

Receipt/log lokal yang sudah diperiksa untuk tabel dan riwayat ini:

- `.local/quality-20261009/opening-freeze-4.json`.
- `.local/fix-2026-10-02/verification/opening-targeted-sqlite-20261010-4.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-full-sqlite-20261010-2.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-targeted-postgres-20261010-5.json` beserta stdout/stderr, dan `.local/quality-20261009/opening-pg-owned-stop-5.json`.
- `.local/quality-20261009/opening-original-public-replay-20261010-4.json` beserta stdout/stderr.
- `.local/quality-20261009/opening-source-org-replay-20261010-1.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-quality-20261010-4.json` beserta stdout/stderr.
- `.local/quality-20261009/opening-browser-result-2.json`, `opening-browser-2/browser-results-2.json`, dan `opening-browser-2/browser-final-readback-2.json`, beserta log serta screenshot perjalanan.
- `.local/quality-20261009/opening-acceptance-1.json`.
- `.local/fix-2026-10-02/verification/opening-targeted-sqlite-20261010-3.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-full-sqlite-20261010-1.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-targeted-postgres-20261010-4.json` beserta stdout/stderr.
- `.local/quality-20261009/opening-original-public-replay-20261010-3.json` beserta stdout/stderr.
- `.local/fix-2026-10-02/verification/opening-quality-20261010-3.json` beserta stdout/stderr.
- `.local/quality-20261009/opening-freeze-3.json`.
- `.local/quality-20261009/opening-source-org-red-20261010-1.json` beserta stdout/stderr.
- `.local/quality-20261009/opening-browser-result-1.json` dan `opening-browser-1/browser-results-1.json`, serta log assertion perjalanan browser.
- PRIMARY: `.local/quality-20261009/opening-company-runtime-20261009T215644Z.json`, `opening-company-preservation-20261009T215644Z.json`, `opening-company-backup-verified-20261009T215644Z.json` dan `opening-company-backup-binding-20261009T215644Z.json`.
- Worktree: `.local/quality-20261009/opening-company-preaction-failure-1.json`, `opening-handoff-approval-2.json`, `opening-handoff-static-review-2.json`, dan log guard baca-saja.

Receipt, sumber sintetis dan log lengkap tetap menjadi bukti lokal. Ringkasan ini tidak menggantikan pembacaan output asli saat penerimaan final.

## Percobaan gagal dan perbaikan yang tetap dicatat

1. Probe publik pertama gagal pada fixture karena `BankAccount` belum dibuat, sebelum membuktikan perilaku fitur. Percobaan ini tetap tersimpan dan tidak dihitung sebagai RED produk.
2. Probe publik kedua menjadi RED yang sah pada source FIN03: modul belum tersedia dan formulir menghasilkan `404`. Probe yang sama kemudian direplay tanpa mengubah assertion atau byte-nya.
3. Candidate SQLite attempt 1 menjalankan 87 kasus: 78 lulus, 7 dilewati, 2 ERROR. Kedua fixture mencoba membuat `AccountingPeriod` yang sudah dibuat oleh perintah kas publik. Perbaikan menggunakan laporan bank dengan kontrol eksplisit dan penutupan bulan melalui layanan publik; assertion pemulihan tetap berlaku.
4. Candidate SQLite attempt 2 menjalankan 91 kasus: 83 lulus, 7 dilewati, 1 assertion gagal. Helper menyiapkan PDF baru setelah baseline jumlah audit sehingga audit unggahan sah ikut dihitung. Persiapan PDF dipindahkan sebelum baseline; assertion bahwa retry/permintaan ditolak tidak menulis tetap dipertahankan. Candidate attempt 3 lulus pada freeze 3, sebagaimana tabel hasil antara di atas.
5. Tinjauan statis menemukan dua P2 yang diperbaiki sebelum penerimaan: graf pemulihan kini mengikuti referensi asli yang disegel bersama referensi saat ini, sehingga mengganti FK tidak memutus identitas bukti asli; pemilihan periode kini memakai tanggal asli dalam payload/snapshot serta tanggal bank/posting/jurnal, sehingga memindahkan seluruh tanggal hidup ke periode mendatang tidak menghapus sumber dari antrean pajak. Regresi mempertahankan hold atas bukti rusak, termasuk pokok tanpa kas dan tanggal asli malformed, sambil menjaga sumber sah setelah cutoff tetap di luar periode lama.
6. P2 ketiga dibuktikan oleh probe asli organisasi pada freeze 3: memindahkan kepemilikan hidup pokok ke organisasi lain membuat sumber asli yang disegel hilang dari inventori Finance. Satu kasus menghasilkan tiga assertion gagal: piutang menjadi `0.00` alih-alih belum dapat dinilai, blocker kosong, dan penutupan publik tidak menolak sumber rusak. Inventori Finance dan pemeriksaan keberadaan sumber laporan kemudian diperbaiki untuk mempertahankan lingkup organisasi asli. Tiga regresi pada freeze 4 lulus: pokok yang dipindahkan tetap belum dapat dinilai dan menahan penutupan nyata; laporan tetap membawa pokok asli sebagai sumber rusak; kas yang dipindahkan tidak hilang dari riwayat pokok asli. Replay probe RED yang sama pada freeze 4 kemudian lulus dengan ketiga assertion asli dan byte probe tetap sama.
7. Browser attempt 1 pada freeze 3 berhenti pada perbedaan format string desimal alat privat, bukan pada assertion yang membuktikan salahnya nominal produk. Hasil gagal, log, screenshot yang sudah diambil, dan penghentian normal tetap disimpan. Perjalanan baru attempt 2 pada freeze 4 kemudian lulus dengan delapan alur dan baca ulang finansial native; attempt 1 tetap dicatat sebagai gagal.
8. Handoff pertama gagal **sebelum penghentian aplikasi, penyalinan source atau migrasi**: `Get-FileHash` tidak tersedia pada PowerShell yang menerima `PSModulePath` warisan. Source FIN03, proses lama dan health `200` tetap utuh saat itu; tidak ada skema atau migrasi baru. Root membuktikan kegagalan melalui diagnosis native terpisah, sedangkan cmdlet tanpa lingkungan modul warisan dan SHA256 .NET berjalan. Helper operasional kedua memakai hash .NET yang setara setelah guard native baca-saja, tinjauan terbatas dan persetujuan root tambahan. Handoff kedua kemudian lulus. Helper/log gagal asli tetap disimpan; perubahan hanya pada helper operasional, bukan source produk atau assertion penerimaan.

Tidak ada percobaan gagal yang dilabel ulang sebagai lulus. Perbaikan fixture tidak melemahkan batas organisasi, peran, kapasitas, tanggal, bukti, retry, maupun audit.

## Pekerjaan yang tetap terbuka

- Koreksi saldo pokok yang salah, koreksi/pembalikan pencocokan kas, dan riwayat persetujuan koreksinya.
- Kas saldo lama yang memiliki pemotongan positif, kredit atau kewajiban pajak historis, dan dokumen pajak yang diperlukan untuk cabang tersebut.
- Saldo awal akun lain di luar piutang/utang lama serta saldo bank yang sudah memiliki alur terpisah.
- Dokumen saldo awal dan transaksi perusahaan nyata yang lengkap; nominal perusahaan tidak diisi dari tebakan atau fixture sintetis.
- UAT staf nonteknis dengan pekerjaan mereka sendiri, deployment cloud/domain/HTTPS, provider live dan backlog ERP lain.

FIN04 ini merupakan satu bagian dari kemampuan keuangan yang diterima dan terpasang lokal. Seluruh ERP tetap belum dinyatakan selesai atau siap produksi; pekerjaan yang masih terbuka tidak ditutup oleh penerimaan alur ini.
