import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'

const AXIS = { stroke: 'var(--faint)', fontSize: 11 }

// The agent proposes a chart; we render it only if the named columns actually exist in the
// result. A hallucinated column name degrades to no chart rather than to a broken one.
export default function ResultChart({ chart, result }) {
  if (!chart || chart.type === 'none' || !result?.rows?.length) return null
  const { columns, rows } = result
  const xi = columns.indexOf(chart.x)
  const yi = columns.indexOf(chart.y)
  if (xi < 0 || yi < 0) return null

  const data = rows.slice(0, 40).map((r) => ({
    x: String(r[xi] ?? ''),
    y: Number(r[yi]),
  })).filter((d) => Number.isFinite(d.y))
  if (data.length < 2) return null

  const Chart = chart.type === 'line' ? LineChart : BarChart
  return (
    <div style={{ padding: '14px 16px 6px', borderTop: '1px solid var(--line)' }}>
      {chart.title && <div style={{ fontSize: 12, color: 'var(--dim)', marginBottom: 8 }}>{chart.title}</div>}
      <ResponsiveContainer width="100%" height={220}>
        <Chart data={data} margin={{ top: 4, right: 8, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="var(--line)" vertical={false} />
          <XAxis dataKey="x" tick={AXIS} interval="preserveStartEnd" angle={-20} textAnchor="end" height={54} />
          <YAxis tick={AXIS} width={64} tickFormatter={(v) => Intl.NumberFormat('en', { notation: 'compact' }).format(v)} />
          <Tooltip
            contentStyle={{ background: 'var(--panel-2)', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
            formatter={(v) => Intl.NumberFormat('en').format(v)}
          />
          {chart.type === 'line'
            ? <Line dataKey="y" stroke="var(--accent)" dot={false} strokeWidth={2} name={chart.y} />
            : <Bar dataKey="y" fill="var(--accent)" name={chart.y} />}
        </Chart>
      </ResponsiveContainer>
    </div>
  )
}
