# Perbaikan audit 2 Oktober 2026

Lingkup pekerjaan ini adalah delapan temuan pada paket audit 2 Oktober. Penanganan dimulai dari aturan data dan transaksi, kemudian formulir dan laporan, lalu pengujian gabungan. Pengujian memakai perusahaan dan transaksi sintetis, bukan pembukuan perusahaan.

## Perubahan alur kerja

| Temuan | Alur setelah perbaikan |
| --- | --- |
| QA-01 — tanggal penyelesaian | Dari rincian invoice, pilih pencatatan selesai, isi tanggal seluruh layanan benar-benar selesai dan referensi buktinya. Pendapatan mengikuti tanggal tersebut. Pembayaran mitra harus sudah tersedia pada tanggal itu, dan periode tertutup tetap terlindungi. Paket kursus yang belum selesai tidak boleh dikonfirmasi selesai seluruhnya. |
| QA-02 — tagihan pemasok berulang | Isi nomor asli pada dokumen pemasok. Nomor internal aplikasi tetap terpisah. Pengiriman ulang formulir yang sama tidak menambah tagihan; nomor pemasok yang sama diperiksa dalam perusahaan dan pemasok yang sama. Dua dokumen berbeda dengan nominal/tanggal sama tetap dapat dicatat. Tagihan lama tanpa nomor asli tetap ditandai belum diketahui. |
| QA-03 — penagihan setelah pelunasan | Daftar tindak lanjut dan saran Marketing memakai sisa tertagih yang memperhitungkan pembayaran serta uang muka yang sudah dialokasikan. Invoice dibatalkan tidak ditagih. Pelunasan melalui uang muka tidak dibuat menjadi penerimaan bank kedua. |
| QA-04 — draf pajak keliru | Finance atau Owner dapat menarik draf mandiri yang belum disetujui dengan alasan yang dicatat. Bukti, nilai asli, pelaku, waktu dan riwayat tetap tersimpan. Draf terkait keputusan tagihan atau penyelesaian pajak tidak dapat dihapus melalui alur ini. Penarikan mengharuskan penyusunan ulang kertas kerja tahunan yang terdampak. |
| QA-05 — PDF pajak | Unggahan pajak memakai pemeriksaan konten PDF yang sama dengan dokumen keuangan, termasuk penolakan konten aktif, lampiran tertanam dan enkripsi. Batas khusus dokumen pajak tetap berlaku. Ini pemeriksaan format/kebijakan dokumen, bukan sertifikasi antivirus. |
| QA-06 — PDF invoice dibatalkan | Sisa tertagih menjadi nol. Nilai historis dan informasi pengembalian dana ditampilkan terpisah agar pembatalan tidak dibaca sebagai tagihan baru. |
| QA-07 — pembayaran budget | Gunakan **Catat pembayaran** pada biaya/komitmen yang sesuai. Pembayaran terhubung pada catatan asal, tanpa menarik biaya yang masih sah. Koreksi jenis catatan tetap tersedia dengan konfirmasi eksplisit bahwa fakta lama ditarik. |
| QA-08 — cakupan biaya iklan | Bedakan kelengkapan nilai pada baris tercatat dari cakupan sumber selama periode laporan. Rasio atas data tercatat tetap diberi label. Konfirmasi cakupan membutuhkan sumber, periode, bukti dan pencocokan total; perubahan data membatalkan keabsahan konfirmasi terkait. Saran eksperimen budget dibatasi sampai cakupan memenuhi syarat. |

## Batas yang tetap berlaku

- Konfirmasi cakupan iklan merupakan pernyataan pengguna berdasarkan bukti, bukan verifikasi langsung oleh Meta atau Google. Kampanye/akun yang belum didaftarkan belum dapat ditemukan otomatis.
- Penarikan draf pajak tidak menggantikan pembetulan dokumen yang sudah disetujui, disetor atau dilaporkan. Persetujuan pajak tetap membutuhkan pemeriksaan profesional yang terdokumentasi.
- Aplikasi belum menyetor atau melaporkan pajak otomatis ke DJP. Perbaikan ini tidak mengubah tarif atau menyatakan kelayakan rezim pajak perusahaan.
- API BNI dan konektor iklan belum diaktifkan melalui pekerjaan ini. Format e-statement nyata tetap membutuhkan uji penerimaan Finance.
- Operasional, penggajian, integrasi pendaftaran, dan deployment cloud/HTTPS merupakan lingkup lanjutan.

## Status verifikasi

Seluruh QA-01 sampai QA-08 selesai dalam lingkup perbaikan ini. Peninjauan silang juga menemukan dan memperbaiki batas panjang hasil normalisasi nomor pemasok, serta penolakan nilai tanggal yang tidak valid pada layanan internal. Hasil final **READY_FOR_SCOPE** berlaku untuk perbaikan tersebut dan regresi terkait; bukan sertifikasi kesiapan seluruh ERP, pajak, atau deployment cloud.

| Pemeriksaan final pada kode yang sama | Hasil |
| --- | --- |
| Seluruh tes Django, PostgreSQL 17.4 terpisah | **395 lulus**, tanpa tes dilewati; termasuk 5 tes konkurensi nyata |
| Seluruh tes Django, SQLite terpisah | **390 lulus**, 5 tes kunci PostgreSQL dilewati dan dijalankan pada baris sebelumnya |
| Tes cadangan dan pemulihan | **5 lulus** |
| Browser Chrome, Owner/Finance/Marketing | **158 pemeriksaan halaman**, desktop 1440 px dan ponsel 390 px, tanpa luapan lebar atau kesalahan JavaScript pada pemeriksaan tersebut |
| Alur browser | **10 alur Finance sebelumnya + 8 alur perbaikan + pemeriksaan pembatasan peran** lulus |
| Nilai tersimpan setelah browser | Pendapatan masuk September sesuai tanggal aktual; tagihan pemasok satu setelah retry; pajak ditarik dengan riwayat; biaya budget tetap aktif setelah pembayaran; PDF dibatalkan bersaldo tertagih nol |
| Konsistensi migrasi, pemeriksaan Django, dependensi, lint kritis | Lulus |
| Pemulihan cadangan dan migrasi salinan data perusahaan | Berhasil, tanpa perubahan isi kolom bisnis lama |
| Lima migrasi pada aplikasi lokal | Berhasil; integritas database dan dokumen privat terjaga, tanpa transaksi bisnis buatan |
| Aplikasi lokal setelah dinyalakan | Halaman masuk, health dan readiness merespons HTTP 200 |

Pemeriksaan UI dan transaksi memakai data sintetis. Data perusahaan hanya dipakai untuk cadangan, uji pemulihan/migrasi salinan, dan migrasi struktur yang telah diuji. Aplikasi lokal sengaja dibiarkan berjalan; server pengujian dihentikan. Tidak ada panggilan provider, pembayaran, pelaporan pajak atau deployment cloud dalam pekerjaan ini.

Bukti privat tersimpan di `.local/fix-2026-10-02/`, termasuk cadangan `pre-rollout.zip`, percobaan gagal sebelum perbaikan, pengujian final, tangkapan layar, pemeriksaan data sebelum/sesudah, dan `gate-final-01/report.md`. File bukti, database dan dokumen asli tidak dimasukkan ke Git. Kontrak final dikunci sebelum pengujian akhir; pemeriksa paket memvalidasi bukti dan cakupan, bukan menggantikan pengujian atau tinjauan profesional.

Identitas isi kode yang diuji: `cf13847f4e48a8e32757dfa491adcf7eaab4e8b4d18b83eeadca30d32252f365` (manifest sumber pengujian, mengecualikan dokumentasi dan lampiran privat).
