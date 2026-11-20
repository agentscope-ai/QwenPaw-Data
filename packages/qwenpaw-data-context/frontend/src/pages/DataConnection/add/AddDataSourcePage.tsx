import { ROUTES } from '@/router';
import React from 'react';
import { Navigate } from 'react-router-dom';

/** Reuse the V4 data-source drawer instead of keeping a second legacy form. */
const AddDataSourcePage: React.FC = () => (
  <Navigate replace to={`${ROUTES.DATA_CONNECTION}?action=add`} />
);

export default AddDataSourcePage;
