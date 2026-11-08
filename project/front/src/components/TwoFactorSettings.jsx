import { useState } from 'react'
import { accountApi } from '../api'
import { errorMessage } from '../messages'
import CopyButton from './CopyButton'
import Notice from './Notice'
import PasswordField from './PasswordField'

function downloadCodes(codes) {
  const blob = new Blob([`Коды восстановления (каждый можно использовать один раз):\n\n${codes.join('\n')}\n`], {
    type: 'text/plain;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'recovery-codes.txt'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function TwoFactorSettings({ user, onUser }) {
  const [setup, setSetup] = useState(null) // { secret, setup_token, qr }
  const [recoveryCodes, setRecoveryCodes] = useState([])
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = async (action) => {
    setError('')
    setBusy(true)
    try {
      await action()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const startSetup = () =>
    run(async () => {
      const [result, { default: QRCode }] = await Promise.all([accountApi.setup2fa(), import('qrcode')])
      // rendered locally: the TOTP secret never leaves the browser for a third-party QR service
      const qr = await QRCode.toDataURL(result.otpauth_uri, { margin: 1, width: 200 })
      setSetup({ ...result, qr })
      setCode('')
    })

  const enable = (e) => {
    e.preventDefault()
    run(async () => {
      const result = await accountApi.enable2fa(setup.setup_token, code.trim())
      setRecoveryCodes(result.recovery_codes)
      setSetup(null)
      setCode('')
      onUser(result.user)
    })
  }

  const disable = (e) => {
    e.preventDefault()
    run(async () => {
      const result = await accountApi.disable2fa(password, code.trim())
      setPassword('')
      setCode('')
      onUser(result.user)
    })
  }

  if (recoveryCodes.length > 0) {
    return (
      <section className="card">
        <h2>Двухфакторная аутентификация включена</h2>
        <p>
          Сохраните коды восстановления в надёжном месте, например в менеджере паролей. Каждый код срабатывает один раз
          и пригодится, если телефон с приложением будет потерян. Больше они показаны не будут.
        </p>
        <ul className="codes">
          {recoveryCodes.map((item) => (
            <li key={item}>
              <code>{item}</code>
            </li>
          ))}
        </ul>
        <div className="actions">
          <CopyButton text={recoveryCodes.join('\n')} label="Копировать коды" className="button" />
          <button type="button" className="button" onClick={() => downloadCodes(recoveryCodes)}>
            Скачать .txt
          </button>
          <button type="button" className="button button-primary" onClick={() => setRecoveryCodes([])}>
            Коды сохранены
          </button>
        </div>
      </section>
    )
  }

  if (user.mfa_enabled) {
    return (
      <section className="card">
        <h2>Двухфакторная аутентификация</h2>
        <p>
          <span className="badge badge-ok">Включена</span> Осталось кодов восстановления: {user.recovery_codes_left}.
        </p>
        {user.recovery_codes_left <= 2 && (
          <Notice type="info">Кодов восстановления почти не осталось. Отключите и снова включите 2FA, чтобы получить новые.</Notice>
        )}
        <form onSubmit={disable} className="form">
          <PasswordField label="Пароль" value={password} onChange={setPassword} autoComplete="current-password" />
          <div className="field">
            <label htmlFor="mfa-disable-code">Код из приложения или код восстановления</label>
            <input id="mfa-disable-code" autoComplete="one-time-code" value={code} maxLength={20} onChange={(e) => setCode(e.target.value)} required />
          </div>
          <Notice>{error}</Notice>
          <button type="submit" className="button button-danger" disabled={busy}>
            Отключить 2FA
          </button>
        </form>
      </section>
    )
  }

  return (
    <section className="card">
      <h2>Двухфакторная аутентификация</h2>
      <p className="muted">
        Помимо пароля при входе понадобится одноразовый код из приложения (Google Authenticator, Aegis, 2FAS, Bitwarden и
        др.). Даже если пароль утечёт, без телефона войти не получится.
      </p>
      {setup ? (
        <form onSubmit={enable} className="form">
          <ol className="steps">
            <li>Отсканируйте QR-код в приложении-аутентификаторе.</li>
            <li>Введите 6-значный код, который покажет приложение.</li>
          </ol>
          <img className="qr" src={setup.qr} alt="QR-код для приложения-аутентификатора" width="200" height="200" />
          <p className="hint">
            Не получается отсканировать? Введите ключ вручную: <code className="secret">{setup.secret.match(/.{1,4}/g).join(' ')}</code>
          </p>
          <div className="field">
            <label htmlFor="mfa-enable-code">Код из приложения</label>
            <input
              id="mfa-enable-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              required
            />
          </div>
          <Notice>{error}</Notice>
          <div className="actions">
            <button type="submit" className="button button-primary" disabled={busy}>
              Включить
            </button>
            <button type="button" className="button button-ghost" onClick={() => setSetup(null)}>
              Отмена
            </button>
          </div>
        </form>
      ) : (
        <>
          <Notice>{error}</Notice>
          <button type="button" className="button button-primary" onClick={startSetup} disabled={busy}>
            Настроить
          </button>
        </>
      )}
    </section>
  )
}
