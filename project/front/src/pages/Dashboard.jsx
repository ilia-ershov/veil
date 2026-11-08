import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { accountApi, aliasApi } from '../api'
import AliasItem from '../components/AliasItem'
import Notice from '../components/Notice'
import SearchBox from '../components/SearchBox'
import { useFlashMessage } from '../flash'
import { matchesQuery } from '../format'
import { errorMessage } from '../messages'

export default function Dashboard() {
  const created = useFlashMessage('created')
  const [user, setUser] = useState(null)
  const [aliases, setAliases] = useState(null) // null while loading
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState(created ? `Адрес ${created} создан.` : '')

  useEffect(() => {
    let cancelled = false
    Promise.all([accountApi.get(), aliasApi.list('active')])
      .then(([account, list]) => {
        if (cancelled) return
        setUser(account.user)
        setAliases(list.aliases)
      })
      .catch((err) => {
        if (cancelled) return
        setError(errorMessage(err, 'Не удалось загрузить адреса.'))
        setAliases([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const save = async (alias, changes) => {
    const { alias: updated } = await aliasApi.update(alias.id, changes)
    setAliases((list) => list.map((item) => (item.id === updated.id ? updated : item)))
  }

  const deactivate = async (alias) => {
    if (!window.confirm(`Отключить ${alias.address}? Письма на него перестанут приходить. Адрес можно будет включить снова.`)) return
    setError('')
    try {
      await aliasApi.deactivate(alias.id)
      setAliases((list) => list.filter((item) => item.id !== alias.id))
      setNotice(`Адрес ${alias.address} отключён. Он появится в разделе «Отключённые».`)
    } catch (err) {
      setError(errorMessage(err, 'Не удалось отключить адрес.'))
    }
  }

  const visible = (aliases || []).filter((alias) => matchesQuery(alias, query))

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Мои адреса</h1>
          {user && (
            <p className="muted">
              Письма пересылаются на <strong>{user.email}</strong>
            </p>
          )}
        </div>
        <Link to="/create" className="button button-primary">
          Создать адрес
        </Link>
      </div>

      <Notice type="success">{notice}</Notice>
      <Notice>{error}</Notice>

      {aliases === null ? (
        <p className="muted">Загрузка…</p>
      ) : aliases.length === 0 ? (
        <div className="empty">
          <p>У вас пока нет активных адресов.</p>
          <p className="muted">
            Создайте отдельный адрес для каждого сайта: так вы узнаете, кто передал вашу почту спамерам, и сможете
            отключить её в один клик.
          </p>
        </div>
      ) : (
        <>
          {aliases.length > 5 && <SearchBox value={query} onChange={setQuery} />}
          <ul className="alias-list">
            {visible.map((alias) => (
              <AliasItem key={alias.id} alias={alias} onSave={save}>
                <button type="button" className="button button-small button-ghost button-danger" onClick={() => deactivate(alias)}>
                  Отключить
                </button>
              </AliasItem>
            ))}
          </ul>
          {visible.length === 0 && <p className="muted">Ничего не найдено.</p>}
        </>
      )}
    </section>
  )
}
