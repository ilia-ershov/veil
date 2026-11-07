import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { accountApi } from '../api'
import { clearToken, setToken } from '../auth'
import EmailVerificationField from '../components/EmailVerificationField'
import Notice from '../components/Notice'
import PasswordField from '../components/PasswordField'
import TwoFactorSettings from '../components/TwoFactorSettings'
import { errorMessage } from '../messages'
import { isStrongPassword, PASSWORD_HINT } from '../validation'

export default function Settings() {
  const [user, setUser] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    accountApi
      .get()
      .then((result) => !cancelled && setUser(result.user))
      .catch((err) => !cancelled && setError(errorMessage(err, 'Не удалось загрузить настройки.')))
    return () => {
      cancelled = true
    }
  }, [])

  if (!user) {
    return (
      <section className="card">
        <h1>Настройки</h1>
        {error ? <Notice>{error}</Notice> : <p className="muted">Загрузка…</p>}
      </section>
    )
  }

  return (
    <div className="stack settings">
      <section className="card">
        <h1>Настройки</h1>
        <dl className="profile">
          <dt>Имя</dt>
          <dd>{user.name}</dd>
          <dt>Основной адрес</dt>
          <dd>{user.email}</dd>
        </dl>
      </section>
      <ChangeEmail user={user} onUser={setUser} />
      <ChangePassword />
      <TwoFactorSettings user={user} onUser={setUser} />
      <Sessions />
      <DeleteAccount />
    </div>
  )
}

function ChangeEmail({ user, onUser }) {
  const [email, setEmail] = useState('')
  const [verification, setVerification] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  const changeEmail = (value) => {
    setEmail(value)
    setVerification('')
  }

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    setDone('')
    if (!verification) return setError('Подтвердите новый адрес кодом из письма.')
    setBusy(true)
    try {
      const payload = { email: email.trim(), verification_token: verification, password }
      if (user.mfa_enabled) payload.code = code.trim()
      const result = await accountApi.changeEmail(payload)
      onUser(result.user)
      setDone(`Основной адрес изменён на ${result.user.email}. Уведомление отправлено на прежний адрес.`)
      setEmail('')
      setVerification('')
      setPassword('')
      setCode('')
    } catch (err) {
      setError(errorMessage(err, 'Не удалось изменить адрес.'))
      if (err.response?.data?.code === 'invalid_verification') setVerification('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Сменить основной адрес</h2>
      <p className="muted">
        Сейчас письма пересылаются на {user.email}. После смены они будут приходить на новый адрес, а адреса на сайтах
        менять не придётся.
      </p>
      <form onSubmit={submit} className="form">
        <EmailVerificationField
          label="Новый адрес"
          purpose="change_email"
          email={email}
          onEmailChange={changeEmail}
          token={verification}
          onVerified={setVerification}
        />
        <PasswordField label="Текущий пароль" value={password} onChange={setPassword} autoComplete="current-password" />
        {user.mfa_enabled && (
          <div className="field">
            <label htmlFor="email-change-code">Код из приложения или код восстановления</label>
            <input id="email-change-code" autoComplete="one-time-code" value={code} maxLength={20} onChange={(e) => setCode(e.target.value)} required />
          </div>
        )}
        <Notice type="success">{done}</Notice>
        <Notice>{error}</Notice>
        <button type="submit" className="button button-primary" disabled={busy || !verification}>
          Сменить адрес
        </button>
      </form>
    </section>
  )
}

function ChangePassword() {
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    setDone('')
    if (!isStrongPassword(password)) return setError(PASSWORD_HINT)
    if (password !== confirm) return setError('Пароли не совпадают.')
    setBusy(true)
    try {
      const result = await accountApi.changePassword({ current_password: current, new_password: password })
      setToken(result.token) // this session stays signed in, all others are revoked
      setDone('Пароль изменён. На остальных устройствах нужно войти заново.')
      setCurrent('')
      setPassword('')
      setConfirm('')
    } catch (err) {
      setError(errorMessage(err, 'Не удалось изменить пароль.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <h2>Сменить пароль</h2>
      <form onSubmit={submit} className="form">
        <PasswordField label="Текущий пароль" value={current} onChange={setCurrent} autoComplete="current-password" />
        <PasswordField label="Новый пароль" value={password} onChange={setPassword} showRules />
        <PasswordField label="Повторите новый пароль" value={confirm} onChange={setConfirm} />
        <Notice type="success">{done}</Notice>
        <Notice>{error}</Notice>
        <button type="submit" className="button button-primary" disabled={busy}>
          Сменить пароль
        </button>
      </form>
    </section>
  )
}

function Sessions() {
  const navigate = useNavigate()
  const [error, setError] = useState('')

  const logoutEverywhere = async () => {
    if (!window.confirm('Завершить все сеансы, включая этот?')) return
    try {
      await accountApi.logoutEverywhere()
      clearToken()
      navigate('/login', { replace: true, state: { message: 'Все сеансы завершены. Войдите снова.' } })
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  return (
    <section className="card">
      <h2>Сеансы</h2>
      <p className="muted">Если вы входили с чужого устройства или подозреваете взлом, завершите все сеансы.</p>
      <Notice>{error}</Notice>
      <button type="button" className="button" onClick={logoutEverywhere}>
        Выйти на всех устройствах
      </button>
    </section>
  )
}

function DeleteAccount() {
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [understood, setUnderstood] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await accountApi.remove(password)
      clearToken()
      navigate('/', { replace: true, state: { message: 'Аккаунт удалён. Все адреса отключены, данные стёрты.' } })
    } catch (err) {
      setError(errorMessage(err, 'Не удалось удалить аккаунт.'))
      setBusy(false)
    }
  }

  return (
    <section className="card card-danger">
      <h2>Удалить аккаунт</h2>
      <p className="muted">
        Все ваши адреса перестанут принимать почту, а данные аккаунта будут удалены. Отменить это действие нельзя.
      </p>
      <form onSubmit={submit} className="form">
        <PasswordField label="Пароль" value={password} onChange={setPassword} autoComplete="current-password" />
        <label className="checkbox">
          <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
          Я понимаю, что аккаунт и все адреса будут удалены безвозвратно
        </label>
        <Notice>{error}</Notice>
        <button type="submit" className="button button-danger" disabled={busy || !understood}>
          Удалить аккаунт
        </button>
      </form>
    </section>
  )
}
