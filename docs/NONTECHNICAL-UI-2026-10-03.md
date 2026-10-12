# Tampilan OSEE untuk staf nonteknikal

Tujuan: staf mengenali pekerjaan, tindakan berikutnya, dan akibatnya dari halaman yang sedang dibuka. Panduan panjang bukan syarat memakai aplikasi. Pemahaman sekali lihat perlu dibuktikan bersama staf asli; pengujian otomatis memeriksa fungsi dan tampilan, bukan pemahaman manusia.

## Aturan untuk setiap modul sekarang dan berikutnya

- Mulai dari tugas tiap peran. Marketing masuk ke Pekerjaan Hari Ini; Finance masuk ke ringkasan keuangan; Owner/Direktur masuk ke ringkasan perusahaan. Tujuan yang dipilih sebelum masuk tetap dihormati bila aman.
- Tampilkan lima atau enam menu utama dengan istilah akrab. Menu tambahan dapat dibuka melalui Menu lainnya; menu itu terbuka otomatis bila halaman aktif berada di dalamnya.
- Utamakan satu tindakan. Beri judul berupa pekerjaan dan kalimat singkat yang menjelaskan langkah berikutnya. Fitur mendatang, penjelasan akses, filter tambahan, riwayat, dan rincian pemeriksaan tidak bersaing dengan pekerjaan utama.
- Gunakan label terlihat, tanda kolom wajib, tanggal yang dapat dipilih, angka rupiah yang jelas, serta pesan kesalahan di dekat kolomnya. Bagian tertutup yang mengandung kesalahan atau masukan wajib bermasalah terbuka otomatis.
- Tampilkan konsekuensi sebelum konfirmasi: draf invoice belum diterbitkan; pembayaran belum berarti layanan selesai; pengesahan bukti belum berarti transfer atau pelaporan DJP; target aktif belum berarti target tercapai.
- Data yang belum tersedia tetap dinyatakan belum tersedia. Jangan menggantinya dengan nol, skor kesiapan, pendapatan, margin, atau hasil pajak yang tidak didukung sumber.
- Gunakan tombol sekurangnya 44 piksel pada alur utama dan input 16 piksel. Menu dan bagian tambahan bekerja lewat keyboard. Di ponsel, pekerjaan harian menjadi kartu dan dapat dibuka dari namanya tanpa menggulir tabel ke kanan.
- Jangan menghapus pemeriksaan sumber, pemisahan wewenang, konfirmasi nominal, akses dokumen privat, alasan koreksi, maupun riwayat demi mempersingkat tampilan.

## Perubahan yang diterapkan

Tampilan bersama mempertahankan logo dan warna OSEE. Pilihan bagian menampilkan bagian yang dapat dipakai, dengan bagian utama akun ditempatkan dahulu. Panduan penyiapan menonjolkan satu langkah berikutnya, kemudian menyimpan daftar lengkap dalam bagian yang dapat dibuka.

Marketing menampilkan tugas harian, calon pelanggan, tugas/bukti kerja, target/hasil, dan iklan/kanal. Pilihan antrean memakai kalimat biasa. Jumlah per jenis pekerjaan, detail iklan, dan beberapa riwayat dapat dibuka saat diperlukan.

Finance mempunyai petunjuk impor bank yang konsisten: pilih file, periksa isi, konfirmasi, periksa saldo, cocokkan pembayaran. Invoice, tagihan, pajak, dan pemeriksaan sumber menjelaskan tindakan berikutnya. Tarif pajak tersimpan sebagai pecahan tetap memakai perhitungan lama; tampilannya menggunakan persen, misalnya nilai tersimpan 0,02 ditampilkan 2%. Angka ini hanya contoh format, bukan penetapan tarif perusahaan.

Direktur melihat tindakan keputusan, kas, dan marketing dahulu. Rincian pesanan/peserta/margin dapat dibuka saat dibutuhkan; antrean persetujuan dan bukti tetap terlihat. Operations menjelaskan langkah unggah, penagihan, penjadwalan, kehadiran, dan penyelesaian peserta berdasarkan status sebenarnya.

## Bukti pemeriksaan

Semua pemeriksaan memakai database sintetis terpisah. Tidak menambahkan staf, transaksi, target, atau kebijakan ke perusahaan asli. Tidak mengubah perhitungan pajak/keuangan atau mengaktifkan layanan eksternal.

- Suite SQLite: 540 kasus ditemukan; 527 lulus dan 13 tes khusus konkurensi PostgreSQL dilewati. Tes khusus tersebut merupakan area pembukuan yang tidak diubah pada pekerjaan tampilan ini; hasil PostgreSQL implementasi sebelumnya tercatat dalam [laporan implementasi](WORKFLOW-IMPLEMENTATION-2026-10-02.md).
- Browser Marketing: 24 pemeriksaan desktop/ponsel dan lima alur persetujuan target, bukti, serta akses lulus.
- Browser Operations: 14 pemeriksaan desktop/ponsel dan tiga alur peserta/invoice/akses lulus.
- Browser tampilan bersama: 40 pemeriksaan halaman dan sembilan alur; masuk sesuai peran, navigasi keyboard, menu aktif, masukan wajib, navigasi ponsel, batas akses, dan membuka pekerjaan harian tanpa menggulir ke samping lulus.
- Pemeriksaan migrasi tidak menemukan perubahan skema. Pemeriksaan template dan konfigurasi lulus.

Bukti otomatis dan tangkapan layar tersimpan privat di `.local/fix-2026-10-02/verification/simple-ux-20261003-*`, `.local/marketing-performance-qa/run-nxehuh4u`, `.local/operations-qa/run-ey03kqtd`, dan `.local/simple-ui-qa/`. Perbaikan tampilan terakhir diperiksa kembali melalui pengujian terkait dan browser. Cloud/HTTPS dan penerimaan staf asli tetap merupakan tahapan terpisah.
