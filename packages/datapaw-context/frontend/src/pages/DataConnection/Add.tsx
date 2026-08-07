import { useEffect } from "react";
import { Spin } from "@/design";
import { useNavigate } from "react-router";
import { ROUTES } from "@/router";

/** Backward-compatible route: all create flows now open from the right drawer. */
export default function DataConnectionAddRedirect() {
  const navigate = useNavigate();

  useEffect(() => {
    navigate(`${ROUTES.DATA_CONNECTION}?action=add`, { replace: true });
  }, [navigate]);

  return <Spin />;
}
