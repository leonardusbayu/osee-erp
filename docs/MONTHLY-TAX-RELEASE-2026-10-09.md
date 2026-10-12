# Paket sumber pajak bulanan OSEE

Finance sekarang dapat memilih bulan dan menyiapkan data dari transaksi yang tercatat. Setelah hasil pemeriksaan profesional dicatat dan disetujui, draf pajak final **setor sendiri yang didukung** memakai angka tersebut tanpa mengetik ulang total. Alamat lokal: [Data pajak bulanan](http://127.0.0.1:8001/tax/monthly/sources/).

Ini penerimaan sebagian kemampuan TAX-03, bukan penutupan seluruh kebutuhan ERP atau sertifikasi perlakuan pajak PT Langkah Pintar Nusantara. [Daftar pekerjaan seluruh aplikasi](FULL-APP-STATUS-2026-10-09.md) tetap menjadi acuan.

## Alur staf

1. Finance membuka **Pajak → Siapkan data bulanan**, memilih bulan, lalu menyiapkan paket. Transaksi dan jurnal perusahaan tidak diubah oleh langkah ini.
2. Layar memisahkan pendapatan dalam buku, sumber yang perlu diperiksa, dan dasar pajak yang sudah diperiksa. Invoice, layanan, pembayaran, uang muka, koreksi dan refund mempunyai tanggal serta nilai masing-masing; uang masuk tidak dijumlahkan lagi sebagai pendapatan.
3. Owner mencatat laporan profesional: perlakuan sumber, selisih sebenarnya yang didukung dokumen, cakupan sumber dan cara pembayaran pajak. Perbedaan dari harga katalog atau biaya pemasok tidak menghasilkan penambahan bruto otomatis.
4. **Buat draf pajak** mengambil hasil keputusan yang disetujui. Retry mengembalikan draf yang sama. Persetujuan kewajiban, pembukuan/pembayaran, dan pencocokan bukti resmi tetap memakai alur masing-masing.
5. Bila hasil pemeriksaan perlu diperbaiki dengan transaksi yang sama, Owner memilih **Perbaiki hasil pemeriksaan**, mencatat alasan dan menyiapkan versi baru. Draf aktif yang terkait harus ditarik lebih dulu melalui alur penarikan yang menyimpan riwayat. Versi baru memerlukan laporan yang terikat pada versi tersebut.

Rincian perhitungan menampilkan pendapatan yang dimasukkan, setiap penyesuaian bertanda positif/negatif, alasan, sumber invoice dan PDF. Angka Rp500.000 + Rp40.000 = Rp540.000 serta draf Rp2.700 yang digunakan pada QA adalah **contoh sintetis**, bukan angka perusahaan.

## Kontrol yang diterapkan

- Snapshot sumber, hasil pemeriksaan dan versi lama tetap tersimpan serta dapat diekspor. CSV merupakan paket sumber/draf internal, bukan XML/SPT resmi atau bukti bayar/lapor.
- Sumber baru bertanggal mundur, perubahan material dokumen, profil, riwayat fiskal atau aturan membuat paket lama perlu diperiksa ulang. Versi yang sudah digantikan tidak dapat membuat atau menyetujui kewajiban baru.
- Alokasi pajak bulan sebelumnya diperiksa agar uang muka dan invoice yang sama tidak dikenakan dua kali. Pemetaan uang muka parsial/bruto khusus yang belum didukung menghentikan proses; aplikasi tidak menebak sisanya.
- Pemeriksaan ulang mempertahankan alokasi lama yang diketahui. Bulan yang bergantung pada pemeriksaan yang masih diperbaiki tidak boleh disetujui dahulu.
- Kewajiban terkait yang disetujui, dibayar atau mempunyai bukti resmi, serta paket tahunan yang disetujui/dilaporkan, memerlukan koreksi formal. Alur koreksi formal tersebut masih terbuka.
- Finance/Owner menyiapkan; Owner mencatat persetujuan laporan. Marketing ditolak. ID perusahaan lain, CSRF tidak sah dan permintaan dengan versi salah tidak dapat mengubah data.
- Profil dan publikasi sumber hukum tetap memerlukan bukti/tinjauan. Omzet di bawah Rp4,8 miliar dan status non-PKP tidak mengaktifkan fasilitas final otomatis.

## Hasil pengujian akhir

Kode bulanan diuji pada identitas **`c442aa1641c728cef9856031a4c99c3af3e3b2b5ef691bd0f16c7624cd28ede3`**. Seluruh runner akhir mencatat identitas awal/akhir yang sama, memakai database dan media terpisah, serta tidak memakai data perusahaan sebagai fixture atau mengaktifkan provider.

| Pemeriksaan | Hasil |
| --- | --- |
| Seluruh suite SQLite | 709 kasus: 684 lulus, 25 khusus PostgreSQL dilewati; 250,203 detik |
| Seluruh suite PostgreSQL 17 | 709/709 lulus, tanpa skip; 401,804 detik, termasuk 25 kasus konkurensi |
| Pemeriksaan independen | 22/22 lulus: integritas riwayat/aturan, uang muka, pengurangan berlebih, peran, versi, koreksi dan perlindungan kewajiban |
| Browser Chrome | 14 alur, 48 keadaan halaman pada lebar 1440/390; tanpa overflow horizontal atau error script; perubahan data tersimpan diverifikasi |
| Skema dan kode | Django check, migration consistency, Ruff untuk error kritis, compile, dependency check lulus |
| Gate bukti | `READY_FOR_SCOPE`, khusus sumber bulanan/draf/koreksi yang ditetapkan; delapan temuan yang direproduksi ditutup |

Bukti privat berada di `.local/quality-20261009/monthly-final-1/`, termasuk plan terkunci, hasil, hash bukti, laporan dan review. PostgreSQL khusus QA serta server browser sementara dihentikan setelah pengujian; bukti sintetis tetap disimpan.

## Pembaruan aplikasi lokal

Server OSEE dihentikan menggunakan peluncur Windows bawaan yang sama dengan tombol Start/Stop. Backup database dan dokumen dibuat ketika server berhenti, kemudian diverifikasi sebelum migrasi `taxes.0009_monthly_source_packages`.

Semua **925 catatan lama pada 115 tabel** dicocokkan per primary key dan kolom lama terhadap database dalam backup dan tetap utuh. Penambahan yang diharapkan adalah dua content type, delapan permission, serta satu sumber dan satu revisi PMK164 yang belum dipublikasikan. Perbandingan hash seluruh tabel pada upaya awal sempat berhenti karena penambahan permission/content type tersebut; pemeriksaan terhadap setiap catatan asli kemudian membuktikan tidak ada catatan lama berubah. Profil pajak dan publikasi perusahaan tetap sama.

Backup: `.local/backups/before-monthly-package-20261009T085218Z.zip`. Bukti preservasi: `.local/quality-20261009/monthly-company-preservation-20261009T085218Z.json`. Setelah restart, tujuh pemeriksaan HTTP untuk health/readiness/login, penjagaan login modul/paket bulanan dan aset lulus. Port8000 aplikasi lain tidak dihentikan; OSEE berjalan di8001.

## Pekerjaan berikutnya

TAX-03 masih memerlukan pemotongan final oleh pelanggan, pemetaan uang muka parsial dan penggunaan kebijakan klasifikasi yang ditinjau agar pemeriksaan berulang tidak menjadi pekerjaan manual per transaksi. Rekonsiliasi tahunan terperinci, pembetulan resmi dan skema ekspor DJP tetap pekerjaan tersendiri. Data asli yang lengkap, penetapan profesional, UAT staf, cloud HTTPS dan API live belum dibuktikan oleh QA sintetis ini.

[Identitas penerima PPh23](PPH23-RECIPIENT-IDENTITY-RELEASE-2026-10-09.md) telah diterima dalam batch TAX-02 tersendiri. Masalah deserialisasi timestamp JSON pada PowerShell7 telah diperbaiki dan diuji sebagai [komponen peluncur tersendiri](LAUNCHER-POWERSHELL-RELEASE-2026-10-09.md). Bukti browser bulanan di atas tetap terikat pada build yang disebutkan; laporan identitas mencatat pemeriksaan native ulang pada source barunya.
