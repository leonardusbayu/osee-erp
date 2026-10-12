# FIN03: uang muka pemasok, pemakaian dan pengembalian

10 Oktober 2026. Source `34f0d185ddde260b7c9651e9bd1a35e716a1f78e8cbf48e483b0193cf0792923` diterima dalam cakupan FIN03 dan dipasang pada aplikasi lokal perusahaan. Akses melalui [uang muka pemasok](http://127.0.0.1:8001/finance/supplier-advances/), dengan login Owner atau Finance. Penerimaan ini tidak menyatakan seluruh ERP siap produksi.

## Alur yang tersedia

Finance memilih pemasok dan uang keluar yang sudah terjadi di bank, memeriksa jumlah serta bukti, lalu mengonfirmasi penyimpanan. Saldo dapat dipakai untuk tagihan pemasok yang didukung atau dikembalikan melalui mutasi bank masuk. Owner dapat membalik seluruh pemakaian dengan alasan, bukti dan tanggal terbuka; catatan lama tetap ada.

Pemakaian mengurangi uang muka dan utang, tanpa pembayaran bank atau biaya kedua. Pengembalian mengurangi uang muka dan menambah kas. Saldo bertanggal, kapasitas mutasi, sumber bank/jurnal, pemasok/tagihan dan organisasi divalidasi. Sumber rusak berarti perlu diperiksa, bukan dianggap nol. Simpan ulang dengan fakta yang persis sama tidak menggandakan jurnal; perubahan bersamaan mempertahankan isian dan meminta pemeriksaan ulang.

Contoh sintetis browser: uang muka Rp1.000, pemakaian Rp400 + Rp200, pengembalian Rp150 + Rp250, pembalikan penuh Rp400 dan pemakaian ulang Rp400. Hasil akhir: uang muka Rp0, utang tagihan Rp0, pemakaian neto Rp600 dan pengembalian Rp400; tujuh kejadian tetap tercatat. Angka tersebut bukan transaksi perusahaan.

Anggaran Direktur mengacu pada kas asli dan pemakaian efektif. Kas dihitung sekali, memakai tanggal bank serta tanggal hubungan tersedia; pengembalian tidak dianggap pendapatan anggaran. Koreksi hubungan anggaran dilakukan secara eksplisit sebelum pembalikan bila diperlukan.

## Pajak dan batas kemampuan

Pembayaran awal tetap mempunyai pemeriksaan pajak tersendiri dalam sumber bulanan/tahunan, termasuk setelah sisa uang muka nol. Keputusan atas tagihan tidak otomatis menggantikan keputusan atas pembayaran awal. Bukti yang belum lengkap atau berubah menahan persetujuan fiskal/tutup buku yang bergantung padanya.

Cabang yang dapat dituntaskan dalam batch ini memerlukan laporan profesional yang benar-benar diterima, keputusan tanpa pemotongan dan nilai potongan nol yang dinyatakan secara eksplisit. Bruto, dasar, tarif atau pajak yang belum diketahui tetap kosong. Keputusan positif atau belum jelas tetap ditahan untuk implementasi berikutnya; AI dan checkbox staf tidak menentukan perlakuan pajak.

Ini adalah kontrol sumber dan keputusan, bukan verifikasi lengkap seluruh perhitungan pajak perusahaan. Pemotongan positif uang muka, pembalikan sebagian, koreksi pembayaran awal/refund, XML/SPT resmi dan pengajuan DJP berada di luar penerimaan ini. Dokumen perusahaan dan aturan yang digunakan masih memerlukan review berbukti.

## Akses dan tampilan

Owner/Finance dapat mencatat, memakai dan mencocokkan pengembalian; pembalikan hanya Owner. Auditor membaca tanpa mengubah, Marketing ditolak. Formulir memakai tiga langkah berlabel dan mempertahankan isian saat sumber berubah. Staf tidak memasukkan JSON atau sidik sumber. Di ponsel, daftar dan riwayat menjadi kartu dengan label, dan teks panjang membungkus. Browser menguji Finance tanpa JavaScript; kemudahan pertama kali bagi staf nyata masih membutuhkan UAT.

## Bukti aktual

| Pemeriksaan | Hasil |
|---|---|
| Regresi lengkap SQLite | 1.068 kasus: 1.014 lulus, 54 skip sesuai lingkungan |
| FIN03 SQLite | 76 kasus: 70 lulus, 6 khusus PostgreSQL skip |
| FIN03 PostgreSQL | 76 lulus, tanpa skip; pembongkaran database uji dan penghentian klaster normal |
| Probe publik asli | 2 lulus; bytes dan assertion probe asli tetap |
| Kualitas | Django check, migrasi, Ruff kategori kesalahan yang dipilih, kompilasi dan dependensi lulus |
| Chrome aktual | 8 alur, 142 keadaan desktop/ponsel; Owner/Finance, auditor pembaca, Marketing ditolak; Finance ponsel tanpa JavaScript; tanpa galat script atau halaman meluber |
| Aplikasi perusahaan setelah pemasangan | 11 pemeriksaan HTTP/aset anonim lulus, termasuk login, readiness, perlindungan rute dan SHA stylesheet yang sama |

Semua bukti akhir memakai source `34f0d…` yang sama. Fixture pengujian sintetis memiliki database/media terpisah dan provider dimatikan. Pemeriksaan kode independen bersifat terbatas dan bukan pengganti bukti native atau UAT.

Kegagalan awal tetap disimpan: oracle fitur belum ada, regresi klaim bank bersama, percobaan tes yang gagal, pemanggilan alat dan containment startup PostgreSQL. Browser pertama menemukan daftar ponsel terlalu lebar; tiga template dan satu CSS khusus diperbaiki. Browser kedua mengukur sebelum CSS selesai; diagnosis setelah load menunjukkan dokumen 390px pada viewport 390px. Browser ketiga tertahan oleh callback animasi pada mode tanpa JavaScript dan berakhir timeout. Runner menghentikan Node karena timeout; WSGI ditutup normal dan Chrome berakhir sehingga tidak diperlukan penghentian paksa proses sisa. Identitas proses perusahaan/PostgreSQL lain tetap sama. Browser keempat memakai load dan pemeriksaan font terbatas lalu pengukuran lebar yang sama, tanpa mengendurkan assertion. Hasil gagal tidak dilabel ulang sebagai lulus.

## Pemasangan dan data perusahaan

Pemasangan dilakukan setelah penerimaan sumber dan pemeriksaan skema sebelum menghentikan proses aplikasi yang terverifikasi. Backup database/unggahan dan arsip sumber dibuat saat aplikasi diam dan diperiksa sebelum 44 file manifest disalin. Hanya tiga migrasi FIN03 dijalankan.

Perbandingan setelah migrasi dan startup mempertahankan seluruh 985 catatan asli pada 125 tabel: kolom/nilai lama, ID/urutan bisnis, skema yang tidak berubah, timestamp migrasi lama, pengguna/hak akses dan unggahan. Hubungan baru pada catatan anggaran lama tetap NULL. Lima tabel bisnis baru kosong. Tambahan hanya 25 definisi framework dan 3 catatan migrasi; nol transaksi bisnis atau data demo ditambahkan.

Backup lokal: `before-supplier-advances-20261009T205901Z.zip` serta `before-supplier-advances-source-20261009T205901Z.zip` di `.local/backups`. Receipt preservasi, hash backup/sumber, acceptance5, seluruh log/screenshot dan gagal sebelumnya disimpan privat pada `.local/quality-20261009` dan verification. Snapshot asli dan kredensial uji tidak dipublikasikan.

Aplikasi lokal berjalan kembali pada port 8001. Pemeriksaan ini tidak memakai login atau transaksi perusahaan; uji provider live dan deployment cloud/HTTPS belum dinyatakan selesai.

## Pekerjaan berikutnya

FIN04: saldo awal piutang/utang dengan rincian pelanggan/pemasok dan pelunasan dari bank nyata, tanpa membuat pendapatan atau biaya periode berjalan. Pemotongan positif uang muka serta seluruh backlog pajak, Direktur, Marketing, operasi, payroll/SDM, portal mitra, UAT dan cloud tetap terbuka. Lihat [status aplikasi](FULL-APP-STATUS-2026-10-09.md).
