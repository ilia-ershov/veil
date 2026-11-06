"""A real browser goes through every user scenario of the specification.

Needs BASE_URL, a code source, and Playwright with Chromium (`playwright install chromium`).
On failure a screenshot is written to live-test-screenshots/.
"""

import re
from contextlib import contextmanager
from pathlib import Path

import pyotp
import pytest

from live_support import count, wait_for

playwright_api = pytest.importorskip("playwright.sync_api", reason="pip install playwright")
expect = playwright_api.expect

PASSWORD = "Str0ng-pass!"
NEW_PASSWORD = "An0ther-pass!"
SCREENSHOTS = Path("live-test-screenshots")


@pytest.fixture
def browser_page(base_url):
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except playwright_api.Error as error:
            pytest.skip(
                f"Chromium is not installed ({error.message.splitlines()[0]}); run: playwright install chromium"
            )
        context = browser.new_context(viewport={"width": 1100, "height": 900}, locale="ru-RU")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
        page = context.new_page()
        errors = []
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())
        yield page, errors
        browser.close()


@contextmanager
def step(page, name):
    print(f"--- {name}")
    try:
        yield
    except Exception as error:
        SCREENSHOTS.mkdir(exist_ok=True)
        path = SCREENSHOTS / (re.sub(r"\W+", "-", name).strip("-") + ".png")
        page.screenshot(path=str(path), full_page=True)
        raise AssertionError(f"step failed: {name} (screenshot: {path})") from error


def code_reader(mailbox, address):
    """Call before triggering the email; the returned function waits for the new code."""
    matches = lambda m: m.to == address.lower() and m.code is not None  # noqa: E731
    before = count(mailbox, matches)
    return lambda: wait_for(mailbox, matches, before=before).code


def test_complete_user_journey(base_url, browser_page, mailbox, new_email):
    page, console_errors = browser_page
    email, new_address = new_email(), new_email()

    with step(page, "a protected page redirects to login"):
        page.goto(f"{base_url}/dashboard")
        expect(page).to_have_url(f"{base_url}/login")

    with step(page, "scenario 1: sign up with email confirmation"):
        page.goto(base_url)
        page.get_by_role("link", name="Создать аккаунт").first.click()
        page.get_by_label("Имя").fill("Тест")
        page.get_by_label("Основной адрес").fill(email)
        read_code = code_reader(mailbox, email)
        page.get_by_role("button", name="Получить код").click()
        page.get_by_label("Код из письма").fill("000000")
        page.get_by_role("button", name="Подтвердить").click()
        expect(page.get_by_text("Неверный или просроченный код. Осталось попыток: 4.")).to_be_visible()
        page.get_by_label("Код из письма").fill(read_code())
        page.get_by_role("button", name="Подтвердить").click()
        expect(page.get_by_text("Адрес подтверждён.")).to_be_visible()
        page.get_by_label("Пароль", exact=True).fill("weak")
        page.get_by_label("Повторите пароль").fill("weak")
        page.get_by_role("button", name="Создать аккаунт").click()
        expect(page.get_by_text("Пароль должен быть не короче 8 символов")).to_be_visible()
        page.get_by_label("Пароль", exact=True).fill(PASSWORD)
        page.get_by_label("Повторите пароль").fill(PASSWORD)
        page.get_by_role("button", name="Создать аккаунт").click()
        expect(page).to_have_url(f"{base_url}/dashboard")
        expect(page.get_by_text("У вас пока нет активных адресов.")).to_be_visible()

    created = []
    with step(page, "scenario 2: create addresses"):
        for service, url in (("GitHub", "github.com"), ("Магазин", "")):
            page.get_by_role("link", name="Создать адрес").click()
            page.get_by_role("button", name="Получить адрес").click()
            generated = page.locator(".generated code")
            expect(generated).not_to_have_text(re.compile(r"^xxxxxxxxxx@"))
            first = generated.inner_text()
            page.get_by_role("button", name="Сгенерировать другой").click()
            expect(generated).not_to_have_text(first)
            address = generated.inner_text()
            assert re.fullmatch(r"[a-z0-9]{10}@[a-z0-9.-]+", address), address
            page.get_by_label("Сервис, для которого этот адрес").fill(service)
            page.get_by_label("Ссылка на сервис (необязательно)").fill(url)
            page.get_by_role("button", name="Создать", exact=True).click()
            expect(page.get_by_text(f"Адрес {address} создан.")).to_be_visible()
            created.append(address)
        expect(page.locator(".alias-list .alias code")).to_have_text(list(reversed(created)))  # newest first
        expect(page.get_by_role("link", name="github.com")).to_have_attribute("href", "https://github.com")

    with step(page, "copy an address to the clipboard"):
        page.locator(".alias").first.get_by_role("button", name="Копировать").click()
        assert page.evaluate("navigator.clipboard.readText()") == created[1]

    row = page.locator(".alias", has_text=created[0])
    with step(page, "scenario 7: edit the service (unsafe link rejected)"):
        row.get_by_role("button", name="Изменить").click()
        row.get_by_label("Ссылка на сервис").fill("javascript:alert(1)")
        row.get_by_role("button", name="Сохранить").click()
        expect(row.get_by_text("Укажите ссылку вида https://example.com.")).to_be_visible()
        row.get_by_label("Сервис", exact=True).fill("GitLab")
        row.get_by_label("Ссылка на сервис").fill("https://gitlab.com")
        row.get_by_role("button", name="Сохранить").click()
        expect(row.get_by_role("link", name="gitlab.com")).to_be_visible()

    with step(page, "scenario 6: deactivate an address"):
        row.get_by_role("button", name="Отключить").click()
        expect(page.get_by_text(f"Адрес {created[0]} отключён.")).to_be_visible()
        expect(page.locator(".alias-list .alias")).to_have_count(1)

    with step(page, "scenario 8: reactivate it"):
        page.get_by_role("link", name="Отключённые", exact=True).click()
        expect(page.locator(".alias-list .alias code")).to_have_text([created[0]])
        page.get_by_role("button", name="Включить снова").click()
        expect(page.get_by_text(f"Адрес {created[0]} снова активен.")).to_be_visible()
        page.get_by_role("link", name="Адреса", exact=True).click()
        expect(page.locator(".alias-list .alias")).to_have_count(2)

    with step(page, "scenario 5: help chapters"):
        page.get_by_role("link", name="Помощь", exact=True).click()
        for title in (
            "Безопасное использование адресов",
            "Фишинговые письма — как не попасться",
            "Пароли — простые правила",
            "Что делать при взломе",
            "Цифровая гигиена",
        ):
            expect(page.get_by_role("heading", name=title)).to_be_visible()

    with step(page, "enable two-factor authentication"):
        page.get_by_role("link", name="Настройки", exact=True).click()
        page.get_by_role("button", name="Настроить").click()
        expect(page.get_by_alt_text("QR-код для приложения-аутентификатора")).to_be_visible()
        totp = pyotp.TOTP(page.locator(".secret").inner_text().replace(" ", ""))
        page.get_by_label("Код из приложения").fill(totp.now())
        page.get_by_role("button", name="Включить", exact=True).click()
        expect(page.locator(".codes code")).to_have_count(10)
        recovery_codes = page.locator(".codes code").all_inner_texts()
        page.get_by_role("button", name="Коды сохранены").click()
        expect(page.get_by_text("Осталось кодов восстановления: 10.")).to_be_visible()

    with step(page, "log in again: wrong password, replayed TOTP code, recovery code"):
        page.get_by_role("button", name="Выйти", exact=True).click()
        page.get_by_label("Основной адрес").fill(email.upper())
        page.get_by_label("Пароль").fill("Wrong-pass1!")
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_text("Неверная почта или пароль.")).to_be_visible()
        page.get_by_label("Пароль").fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_role("heading", name="Подтверждение входа")).to_be_visible()
        page.get_by_label("Код").fill(totp.now())  # already used to enable 2FA
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_text("Неверный код из приложения или код восстановления.")).to_be_visible()
        page.get_by_label("Код").fill(recovery_codes[0])
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page).to_have_url(f"{base_url}/dashboard")

    with step(page, "scenario 3: change the main address"):
        page.get_by_role("link", name="Настройки", exact=True).click()
        section = page.locator("section", has=page.get_by_role("heading", name="Сменить основной адрес"))
        section.get_by_label("Новый адрес").fill(new_address)
        read_code = code_reader(mailbox, new_address)
        is_notice = lambda m: m.to == email.lower() and "изменён" in m.subject  # noqa: E731
        notices_before = count(mailbox, is_notice)
        section.get_by_role("button", name="Получить код").click()
        section.get_by_label("Код из письма").fill(read_code())
        section.get_by_role("button", name="Подтвердить").click()
        section.get_by_label("Текущий пароль").fill(PASSWORD)
        section.get_by_role("button", name="Сменить адрес").click()
        expect(section.get_by_text(f"Основной адрес изменён на {new_address}.")).to_be_visible()
        wait_for(mailbox, is_notice, before=notices_before)  # the old address is told about the change

    with step(page, "change the password, staying signed in"):
        section = page.locator("section", has=page.get_by_role("heading", name="Сменить пароль"))
        section.get_by_label("Текущий пароль").fill(PASSWORD)
        section.get_by_label("Новый пароль", exact=True).fill(NEW_PASSWORD)
        section.get_by_label("Повторите новый пароль").fill(NEW_PASSWORD)
        section.get_by_role("button", name="Сменить пароль").click()
        expect(section.get_by_text("Пароль изменён.")).to_be_visible()
        page.get_by_role("link", name="Адреса", exact=True).click()
        expect(page.locator(".alias-list .alias")).to_have_count(2)

    with step(page, "an invalid session sends the user back to login"):
        page.evaluate("localStorage.setItem('token', localStorage.getItem('token').slice(0, -2) + 'xx')")
        page.reload()
        expect(page).to_have_url(re.compile(r"/login\?expired=1$"))

    with step(page, "reset a forgotten password (2FA still required)"):
        page.get_by_role("link", name="Забыли пароль?").click()
        expect(page.get_by_role("heading", name="Восстановление пароля")).to_be_visible()
        page.get_by_label("Основной адрес").fill(new_address)
        read_code = code_reader(mailbox, new_address)
        page.get_by_role("button", name="Получить код").click()
        page.get_by_label("Код из письма").fill(read_code())
        page.get_by_role("button", name="Подтвердить").click()
        page.get_by_label("Новый пароль").fill(PASSWORD)
        page.get_by_label("Повторите пароль").fill(PASSWORD)
        page.get_by_role("button", name="Сменить пароль").click()
        expect(page.get_by_text("Пароль изменён. Войдите с новым паролем.")).to_be_visible()
        page.get_by_label("Основной адрес").fill(new_address)
        page.get_by_label("Пароль").fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.get_by_label("Код").fill(recovery_codes[1])
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page).to_have_url(f"{base_url}/dashboard")

    with step(page, "mobile menu"):
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Меню").click()
        page.get_by_role("link", name="Настройки", exact=True).click()
        expect(page.get_by_role("link", name="Отключённые", exact=True)).to_be_hidden()  # closes on navigation
        page.set_viewport_size({"width": 1100, "height": 900})

    with step(page, "scenario 4: delete the account"):
        section = page.locator("section", has=page.get_by_role("heading", name="Удалить аккаунт"))
        section.get_by_label("Пароль").fill(PASSWORD)
        expect(section.get_by_role("button", name="Удалить аккаунт")).to_be_disabled()
        section.get_by_label("Я понимаю").check()
        section.get_by_role("button", name="Удалить аккаунт").click()
        expect(page.get_by_text("Аккаунт удалён.")).to_be_visible()
        page.goto(f"{base_url}/login")
        page.get_by_label("Основной адрес").fill(new_address)
        page.get_by_label("Пароль").fill(PASSWORD)
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.get_by_text("Неверная почта или пароль.")).to_be_visible()

    # "Failed to load resource" is the browser logging the API's expected 4xx answers
    unexpected = [error for error in console_errors if "Failed to load resource" not in error]
    assert not unexpected, f"browser console errors: {unexpected}"
