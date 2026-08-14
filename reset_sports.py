from app import SessionLocal
from sports_models import Fixture, Prediction


def reset_sports_data():
    db = SessionLocal()

    deleted_predictions = db.query(Prediction).delete()
    deleted_fixtures = db.query(Fixture).delete()

    db.commit()
    db.close()

    print(f"Deleted {deleted_fixtures} fixtures.")
    print(f"Deleted {deleted_predictions} predictions.")
    print("Users and payments were not touched.")


if __name__ == "__main__":
    reset_sports_data()