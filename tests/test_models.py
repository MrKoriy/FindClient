"""Tests for Organization dataclass."""

from models.organization import Organization
from models import Organization as OrganizationFromInit


class TestOrganization:
    """Test Organization dataclass fields and defaults."""

    def test_create_with_id_and_name_only(self):
        """Organization can be created with only id and name; all other fields use defaults."""
        org = Organization(id="12345", name="Test Company")
        assert org.id == "12345"
        assert org.name == "Test Company"

    def test_phone_defaults_to_empty_string(self):
        """phone defaults to empty string."""
        org = Organization(id="1", name="Test")
        assert org.phone == ""

    def test_email_defaults_to_empty_string(self):
        """email defaults to empty string."""
        org = Organization(id="1", name="Test")
        assert org.email == ""

    def test_website_defaults_to_empty_string(self):
        """website defaults to empty string."""
        org = Organization(id="1", name="Test")
        assert org.website == ""

    def test_address_defaults_to_empty_string(self):
        """address defaults to empty string."""
        org = Organization(id="1", name="Test")
        assert org.address == ""

    def test_rating_defaults_to_zero(self):
        """rating defaults to 0.0."""
        org = Organization(id="1", name="Test")
        assert org.rating == 0.0

    def test_socials_defaults_to_empty_string(self):
        """socials defaults to empty string."""
        org = Organization(id="1", name="Test")
        assert org.socials == ""

    def test_create_with_all_fields(self):
        """Organization can be created with all fields specified."""
        org = Organization(
            id="12345",
            name="OOO Ромашка",
            phone="+7-495-123-4567",
            email="info@romashka.ru",
            website="https://romashka.ru",
            address="Москва, ул. Тверская, д. 1",
            rating=4.5,
            socials="https://vk.com/romashka",
        )
        assert org.id == "12345"
        assert org.name == "OOO Ромашка"
        assert org.phone == "+7-495-123-4567"
        assert org.email == "info@romashka.ru"
        assert org.website == "https://romashka.ru"
        assert org.address == "Москва, ул. Тверская, д. 1"
        assert org.rating == 4.5
        assert org.socials == "https://vk.com/romashka"

    def test_importable_from_models_init(self):
        """Organization is importable from models package directly."""
        assert OrganizationFromInit is Organization
