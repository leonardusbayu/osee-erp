# Pemetaan sumber pendaftaran OSEE

Ditinjau pada 21 September 2026. Dokumen ini adalah hasil pemeriksaan sumber dan rancangan pemetaan untuk pengembangan. Belum ada impor pendaftaran ke database ERP, perubahan Google Sheet, atau transaksi keuangan yang dibuat dari sumber ini.

## Sumber dan cakupan

- Sumber: [Google Sheet pendaftaran OSEE](https://docs.google.com/spreadsheets/d/1b0yiOV2rV5F4TVt2334OFbsEHexMeEZ2hWKgxlELhKM/edit?usp=sharing).
- Pemilik mengonfirmasi bahwa sumber berisi **campuran pelanggan langsung dan peserta dari mitra**.
- `Sheet1`: **2.643 baris sumber**, baris 2–2644; 23 kolom berjudul (`A:W`), serta satu sel terisi tanpa judul di `X2581`. `Sheet2` kosong.
- Ini jumlah baris pendaftaran mentah, bukan jumlah peserta unik, tes selesai, transaksi, atau pembayaran terverifikasi.
- Sebanyak 2.639 sel tanggal tes terbaca sebagai tanggal: 440 baris bertanggal 2025 dan 2.199 baris bertanggal 2026. Rentang tanggal tersebut adalah 12 Oktober 2025–27 September 2026. Empat sel lainnya perlu pemeriksaan tersendiri.
- Sebanyak 14 baris memiliki tanggal tes setelah tanggal pemeriksaan. Jadwal mendatang sah untuk perencanaan, tetapi bukan bukti penyelesaian layanan.
- Label layanan yang terisi selain pilihan bawaan seluruhnya menyebut TOEFL ITP, termasuk paket persiapan/kursus. Sumber ini belum membuktikan cakupan pendaftaran TOEFL iBT atau kursus mandiri.

Salinan sumber dan profil pemeriksaan disimpan privat di `.local/registration-2026-09-21/`, yang diabaikan Git. SHA-256 snapshot XLSX: `d1b2e4dfc3fafd49533e1bd1fffe05204e11279a9115ab5de5793e00b104b2d5`. Snapshot adalah keadaan saat pengambilan, bukan koneksi sinkronisasi aktif. Tidak ada file KTP atau bukti pembayaran pada tautan yang diunduh atau diverifikasi dalam pemeriksaan ini.

## Temuan yang memengaruhi impor

| Temuan | Bukti sumber | Perlakuan yang diperlukan |
| --- | --- | --- |
| Pembeli langsung dan mitra bercampur, tanpa kolom khusus mitra | Konfirmasi pemilik; header `A1:W1` | Tambahkan jenis pemesan dan pemetaan mitra/pembeli yang dikonfirmasi admin. Jangan menebak dari nama, email, nomor WA, atau survey. |
| Tidak ada ID pendaftaran, ID pesanan, nomor invoice, nominal tagihan/pembayaran, tanggal penerimaan bank, atau referensi bank khusus | Header `A1:W1` | Sediakan hubungan yang diperiksa antara pendaftaran, pemesan, invoice, dan mutasi bank. Jangan membentuk nilai transaksi dari jumlah peserta dikali harga saat ini. |
| 2.629 baris memiliki tautan bukti pembayaran; 14 kosong | `P2:P2644` | Status awal adalah bukti tersedia/belum tersedia. Keberadaan tautan tidak membuktikan keaslian, nilai pembayaran, atau pelunasan. |
| 2.636 baris memiliki tautan KTP; 7 kosong | `O2:O2644` | Simpan referensi sebagai data terbatas. Pengambilan dokumen memerlukan alur privat terotorisasi; jangan membuat tautan KTP terlihat oleh Marketing. |
| 1.844 baris kosong pada kolom STATUS; 799 terisi | `V2:V2644` | Status kosong berarti belum diketahui. 769 variasi “Sudah diproses” harus diklarifikasi maknanya; jangan langsung dipetakan ke lunas, hadir, atau selesai. |
| Kolom promo, add-on, dan tanggal kelas juga berisi catatan operasional | `R:T`, dibandingkan `V:W` | Pisahkan status pembayaran, keikutsertaan tes, pemrosesan administrasi, refund, dan sertifikat. Pertahankan catatan asli serta tandai konflik. |
| Delapan kelompok memiliki isi baris sama setelah spasi tepi dibersihkan | 19 baris dalam kelompok tersebut; 11 baris tambahan jika satu baris per kelompok dipertahankan | Tampilkan kandidat duplikat untuk review. Jangan menghapus sumber atau otomatis menyamakan semua pendaftaran orang yang sama. |
| 134 kelompok berbagi email + tanggal tes + sesi | 290 baris terkait; perbandingan teks tanpa membedakan huruf besar/kecil | Ini sinyal pemeriksaan, bukan bukti duplikasi. Kontak mitra dapat digunakan oleh beberapa peserta. Angka ini tumpang tindih dengan pemeriksaan lain, sehingga tidak boleh dijumlahkan sebagai total duplikat. |
| Empat tanggal tes belum berbentuk tanggal | `B102` hanya tahun; `B222` dan `B1918` kosong; `B223` angka serial 45990 | Serial 45990 konsisten dengan 29 November 2025 pada sistem tanggal workbook, tetapi perlu normalisasi yang tercatat. Jangan mengubah angka tahun 2025 menjadi tanggal serial Excel. |
| Data kontak dan tanggal lahir bercampur tipe angka, teks, dan tanggal Excel | `H:M` | Nomor WA harus menjadi teks setelah normalisasi; jangan menambahkan digit yang sudah hilang. Tanggal lahir harus divalidasi sebagai gabungan bulan, hari, dan tahun, bukan menganggap seluruh kolom sudah bersih. |
| Tiga layanan masih berupa pilihan bawaan | `A2:A2644` | Tahan pemetaan produk sampai admin menentukan layanan. |
| Ada data di kolom tanpa judul | `X2581` | Tampilkan sebagai pengecualian; jangan membuang atau menebak tujuannya. |

Pemeriksaan duplikat di atas bersifat awal. Dua baris yang mirip dapat merupakan koreksi, perubahan jadwal, pendaftaran ulang, atau penggunaan kontak bersama. Keputusan akhir harus meninggalkan jejak review.

## Pemetaan ke aplikasi

Model peserta, pendaftaran, sesi tes, dan pesanan operasional belum tersedia. Modul Operasional juga masih ditandai belum aktif di `core/modules.py`. Jangan memasukkan roster ke model rekap bulanan hanya karena keduanya berasal dari spreadsheet.

| Data sumber | Tujuan yang diusulkan | Hubungan dengan fitur yang sudah tersedia |
| --- | --- | --- |
| `D:M`: identitas dan kontak | `Participant` dengan identitas terbatas, validasi, serta riwayat koreksi | Peserta bukan otomatis pelanggan penagihan pada `finance.Party`. |
| `A:C`: layanan, tanggal, sesi | `Registration` dan `TestSession`, termasuk riwayat pindah jadwal | Gunakan master `finance.Product`; paket tes + kursus membutuhkan komponen layanan yang eksplisit. |
| Pembeli/mitra, belum tersedia sebagai kolom khusus | Pesanan dengan jenis pemesan, `buyer_party_id`, dan kelompok peserta | `finance.Party(kind=reseller)` dapat mewakili mitra setelah dicocokkan dan dikonfirmasi. |
| Hubungan pesanan dan invoice | Alokasi pendaftaran ke baris/kuantitas pesanan dan invoice | Satu invoice mitra dapat membiayai banyak peserta. Jangan membuat satu invoice untuk setiap baris sheet secara otomatis. |
| `P`: bukti pembayaran | Bukti sumber dan antrean pencocokan Finance | Pembayaran diverifikasi melalui e-statement BNI dan layanan alokasi/uang muka yang sudah tersedia. |
| `N`, `O`, `U`: pengiriman dan dokumen | Detail operasional serta penyimpanan terbatas sesuai kebutuhan | Jangan mengambil tautan eksternal secara otomatis saat impor. Validasi akses, jenis file, ukuran, dan tujuan pengambilan terlebih dahulu. |
| `Q:R`: survey dan promo | Jawaban survey asli, kategori marketing, dan kode promo yang dinormalisasi | Survey merupakan atribusi yang dilaporkan peserta. “Instagram” tidak otomatis Meta Ads; “Google” tidak otomatis Google Ads atau SEO. |
| `S:T`: add-on dan kelas | Komponen layanan, jadwal kelas, dan status penyelesaian masing-masing | Pendapatan kursus per sesi masih merupakan pekerjaan lanjutan; selesai tes tidak otomatis menyelesaikan paket kursus. |
| `V:W` serta catatan salah kolom | Status operasional terpisah dan antrean review konflik | “Refund” di sheet adalah laporan sumber; bukan instruksi untuk mengeluarkan uang atau membuat jurnal. |

## Alur impor yang disarankan

1. **Unggah sumber / baca snapshot.** Catat organisasi, sumber, tab, waktu, hash, dan pengunggah. Pertahankan nilai asli serta alamat baris/sel untuk penelusuran. File tetap privat.
2. **Lihat ringkasan.** Tampilkan tahun, jumlah baris, produk, kolom yang dikenali, dan masalah yang harus diperiksa. Pilihan “tahun 2026” memakai tanggal tes yang sudah valid; empat tanggal bermasalah tetap terlihat di antrean terpisah.
3. **Periksa data.** Sajikan kelompok duplikat, tanggal/layanan belum jelas, konflik status, dan kontak bersama. Gunakan bahasa tindakan sederhana seperti “Periksa tanggal”, “Pilih pemesan”, dan “Cocokkan pembayaran”.
4. **Tentukan pemesan.** Cocokkan pelanggan langsung atau mitra ke identitas pembeli yang benar. Dukung satu pesanan untuk banyak peserta, pembayaran gabungan, dan bukti yang dipakai bersama. Admin harus dapat menerapkan pemetaan kelompok dengan pratinjau sebelum menyimpan.
5. **Simpan pendaftaran terverifikasi.** Buat identitas internal tetap. Jika sumber memiliki ID respons dari formulir, gunakan ID itu sebagai kunci eksternal; nomor baris saja tidak stabil saat sheet diurutkan. Hash snapshot menahan pengulangan file identik, sedangkan koreksi lintas-snapshot memerlukan pencocokan dan riwayat perubahan.
6. **Cocokkan Finance.** Hubungkan ke pesanan/invoice dan pembayaran berdasarkan bukti aktual. Gunakan harga historis yang disetujui, bukan tarif ITP saat ini. Pembayaran sebelum invoice memakai alur uang muka yang sudah tersedia.
7. **Catat pelaksanaan layanan.** Kehadiran, gugur, penghentian tes, perubahan jadwal, dan penyelesaian kelas memiliki keadaan terpisah. Aturan pembukuan tetap dijalankan oleh layanan Finance, dengan penutupan periode, audit, dan pembatasan hak akses yang berlaku.

Registrasi tidak menciptakan kas, piutang, pendapatan, atau pajak dengan sendirinya. Pada implementasi Finance saat ini, penerbitan invoice membentuk piutang dan pendapatan ditangguhkan; penerimaan bank terverifikasi dialokasikan; pendapatan dilepas melalui penyelesaian seluruh invoice. Penyelesaian satu peserta tidak boleh melepas seluruh pendapatan invoice mitra yang pesertanya belum selesai. Dukungan penyelesaian sebagian/paket campuran harus dikembangkan sebelum otomatisasi tersebut diaktifkan.

## Pemakaian oleh tim dan AI

- **Finance:** pemesan, harga, invoice, uang muka, bukti pembayaran, rekonsiliasi, serta refund melalui alur yang disetujui.
- **Marketing:** agregat pendaftaran dan sumber informasi yang dilaporkan; akses kontak hanya sesuai lingkup tugas. KTP, bukti transfer, dan tanggal lahir tidak perlu tampil pada dashboard marketing. Satu invoice mitra tidak boleh dihitung sebagai banyak pembelian karena banyak peserta. `marketing.Lead` saat ini mewakili peluang pembelian dan hanya mendukung satu tautan lead per invoice.
- **Owner/Direktur:** pendaftaran mentah, pendaftaran terverifikasi, peserta terjadwal, penyelesaian tes, kas terverifikasi, pendapatan, dan margin ditampilkan sebagai ukuran berbeda. Angka sumber ini belum dapat menggantikan rekap 2026 yang berasal dari sumber lain tanpa rekonsiliasi cakupan dan duplikat.
- **Operasional mendatang:** jadwal, roster, pemeriksaan identitas, kehadiran, perubahan jadwal, hasil dan sertifikat. Hak aksesnya perlu didefinisikan sebelum modul diaktifkan.
- **AI OpenRouter:** gunakan agregat terotorisasi untuk penjelasan tren. Jangan memasukkan seluruh sheet, KTP, tautan bukti transfer, atau isi unggahan akun ke prompt secara otomatis. Jawaban harus menyebut cakupan data serta membedakan pendaftaran dari pendapatan. Data sumber dan catatan peserta adalah data, bukan instruksi bagi AI. AI tidak menetapkan status lunas atau membuat jurnal.

## Informasi yang masih perlu dilengkapi

Yang paling menentukan adalah **sumber pemetaan setiap pendaftaran ke pelanggan langsung atau mitra tertentu**. Selanjutnya diperlukan ID respons formulir jika ada, identitas pesanan/invoice, nominal dan tanggal pembayaran terverifikasi, arti “Sudah diproses”, serta keputusan atas duplikat dan perubahan jadwal. Tanpa itu, sumber ini sudah berguna untuk perencanaan pendaftaran dan review data, tetapi belum cukup untuk otomatisasi pembukuan atau laporan pajak.

Lihat [serah terima developer](DEVELOPER-HANDOFF.md), [panduan Finance](FINANCE-OPERATING-GUIDE.md), dan [hasil perbaikan](REMEDIATION-2026-09-09.md) untuk aturan aplikasi yang harus dipertahankan.
