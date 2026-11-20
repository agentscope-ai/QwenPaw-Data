import { Navigate } from 'react-router-dom';

/** Legacy entry: Raw Cypher now lives in the Graph Center console. */
export default function RawCypherPage() {
  return <Navigate to="/graph-center/global?cypher=raw" replace />;
}
