from cryptography.fernet import Fernet

def main():
    key = Fernet.generate_key()
    print("\n--- KARA ENCRYPTION KEY GENERATOR ---")
    print("------------------------------------------")
    print("Salin baris di bawah ini dan masukkan ke file .env Anda:")
    print(f"\nFERNET_KEY={key.decode()}")
    print("\n------------------------------------------")
    print("CATATAN: Simpan kueri ini aman. Jika kunci ini hilang,")
    print("KARA tidak akan bisa mendekripsi private key wallet user!")

if __name__ == "__main__":
    main()
