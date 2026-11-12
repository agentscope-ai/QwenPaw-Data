/** Legacy search route — design 17 moved search into the global-graph left panel. */
import { Navigate, useLocation } from 'react-router-dom';

export default function SearchPage() {
  const { search } = useLocation();
  return <Navigate to={`/graph-center/global${search}`} replace />;
}
