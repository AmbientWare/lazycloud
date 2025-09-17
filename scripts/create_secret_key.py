from cryptography.fernet import Fernet


def create_secret_key():
    """Create a secret key for the database."""
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    print(create_secret_key())
