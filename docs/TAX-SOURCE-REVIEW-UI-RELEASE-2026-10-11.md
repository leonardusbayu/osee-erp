# Rilis — status dan publikasi sumber pajak

**Cakupan:** antarmuka lokal untuk meninjau rujukan pajak umum dan menerbitkan versi yang telah diperiksa. Perubahan tidak menilai kelayakan, tarif, kewajiban, atau pajak perusahaan.

## Alur staf

Owner dan Finance membuka **Pajak → Periksa status semua rujukan pajak**. Halaman menunjukkan sumber aktif, versi baru yang menunggu pemeriksaan, dan rujukan yang belum siap. Setiap versi calon menampilkan ringkasan, pasal, masa berlaku, tanggal tinjauan, dan tautan dokumen resmi. Finance dapat membaca status dan riwayat, tetapi tidak dapat menerbitkan versi.

Penerbitan memerlukan akun administrator sistem yang juga merupakan anggota Owner atau Finance di organisasi. Formulir meminta versi, nama pemeriksa, catatan bukti, dan konfirmasi bahwa isi sudah dibandingkan dengan sumber resmi. Versi yang kedaluwarsa, tidak lengkap, sudah pernah diterbitkan, atau tidak cocok dengan fingerprint ditolak. Riwayat menyimpan pemeriksa, catatan bukti, akun penerbit, dan waktu; versi sebelumnya tetap tersimpan.

Penerbitan berlaku pada katalog rujukan umum. Proses ini tidak mengubah profil pajak organisasi, transaksi, jurnal, paket pajak, atau laporan. `seed_tax_sources` tetap idempoten untuk versi yang sama; permintaan menerbitkan versi identik secara langsung ditolak. `taxes.knowledge` tetap menjadi interface yang digunakan pemanggil lama, sedangkan penyimpanan versi dan resolusi periode berada di modul terpisah.

## Verifikasi

- `manage.py test taxes --noinput`: **306 lulus, 9 dilewati, 0 gagal/error** setelah tabel riwayat ditambahkan; cakupannya meliputi UI sumber, knowledge, chat, identitas PPh 23, pajak bulanan, dan SPT Tahunan.
- `manage.py check`: bersih.
- `manage.py makemigrations --check --dry-run`: tidak ada migrasi tertunda.
- Ruff pada modul sumber, tes, dan URL: lulus.
- Aislop 0.18.1 pada delapan file pajak terkait: **100/100**, 0 warning dan 0 error.

## Batas

Tinjauan yang tercatat adalah bukti proses operator, bukan pendapat pajak otomatis atau persetujuan isi oleh DJP. Sebelum sumber dipakai untuk keputusan perusahaan, pemeriksa yang kompeten tetap harus memastikan teks resmi, perubahan aturan, masa berlaku, dan kecocokan topik. Sumber global dapat memengaruhi semua organisasi yang memakai katalog bersama. UAT staf dan deployment cloud belum termasuk dalam rilis ini; `all_erp_ready` tetap `false`.
