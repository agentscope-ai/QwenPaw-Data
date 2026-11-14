import { ROUTES } from '@/router';
import React from 'react';
import { Navigate } from 'react-router-dom';

/** V4 uses the strict CRUD drawer on the metric list; the legacy batch wizard sent V3 fields. */
const MetricAdd: React.FC = () => <Navigate replace to={ROUTES.METRIC_LIB} />;

export default MetricAdd;
