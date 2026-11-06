import { Link } from 'react-router-dom'
import Notice from '../components/Notice'
import { MAIL_DOMAIN } from '../config'
import { useFlashMessage } from '../flash'

// What the dashboard looks like after a few registrations (made-up addresses).
const DEMO = [
  { address: 'x7k2m9q4ab', note: 'github.com → ваш ящик' },
  { address: 'p0z8e1n4rq', note: 'интернет-магазин → ваш ящик' },
  { address: 't5w3c7j2ud', note: 'форум, базу которого украли · письма отклоняются', off: true },
]

const FEATURES = [
  {
    title: 'Утечка? Отключите адрес',
    text: 'Если сайт начал слать спам или его базу украли, отключите адрес в один клик. Основная почта останется чистой.',
  },
  {
    title: 'Вас не найти по почте',
    text: 'Случайные адреса нельзя связать между собой, поэтому по утёкшей базе не понять, где ещё вы зарегистрированы.',
  },
  {
    title: 'Смена почты без хлопот',
    text: 'Поменяли основной ящик? Обновите его один раз в настройках, а не на каждом сайте.',
  },
]

export default function Home() {
  const message = useFlashMessage()
  return (
    <div className="stack">
      <Notice type="success">{message}</Notice>
      <section className="hero">
        <div>
          <h1>Отдельный адрес почты для каждого сайта</h1>
          <p className="lead">
            Регистрируйтесь с адресом на <code>{MAIL_DOMAIN}</code> — письма придут в ваш обычный ящик, а настоящий
            адрес сайт не узнает.
          </p>
          <div className="actions">
            <Link to="/signup" className="button button-primary button-large">
              Создать аккаунт
            </Link>
            <Link to="/login" className="button button-large">
              Войти
            </Link>
          </div>
        </div>
        <ul className="demo" aria-label="Пример списка адресов">
          {DEMO.map((item) => (
            <li key={item.address} className={item.off ? 'demo-off' : undefined}>
              <code>
                {item.address}@{MAIL_DOMAIN}
              </code>
              <span>{item.note}</span>
            </li>
          ))}
        </ul>
      </section>
      <section className="features">
        {FEATURES.map((feature) => (
          <article key={feature.title} className="feature">
            <h2>{feature.title}</h2>
            <p>{feature.text}</p>
          </article>
        ))}
      </section>
    </div>
  )
}
