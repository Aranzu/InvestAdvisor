"""El cargador de ``.env``: qué syntaxis acepta y, sobre todo, qué pisa qué.

La precedencia es lo único que hay que acertar acá. Si ``.env`` pisa la variable
de entorno, un archivo viejo de una maquina de desarrollo puede pisar el secreto
bien puesto en produccion sin que nada se entere, y eso es un incidente de
seguridad disfrazado de detalle de implementacion. Por eso estos tests son mas
sobre precedencia que sobre parseo.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scraper.envfile import default_env_path, load_env, parse_env_file, project_root


def _write(texto: str) -> Path:
    folder = Path(tempfile.mkdtemp())
    ruta = folder / ".env"
    ruta.write_text(texto, encoding="utf-8")
    return ruta


class TestParse(unittest.TestCase):
    def test_une_pares_simple(self):
        ruta = _write("DEEPSEEK_API_KEY=sk-abc\nOTRA=1\n")
        self.assertEqual(parse_env_file(ruta), {"DEEPSEEK_API_KEY": "sk-abc", "OTRA": "1"})

    def test_quita_comillas_simples_y_dobles(self):
        ruta = _write('A="con espacio"\nB=\'otro valor\'\n')
        self.assertEqual(parse_env_file(ruta), {"A": "con espacio", "B": "otro valor"})

    def test_ignora_comentarios_y_lineas_vacias(self):
        ruta = _write("# comentario\n\n  \nA=1\n   # indented\n")
        self.assertEqual(parse_env_file(ruta), {"A": "1"})

    def test_tolera_export(self):
        self.assertEqual(parse_env_file(_write("export A=1\n")), {"A": "1"})

    def test_conserva_el_igual_del_dentro_del_valor(self):
        # Una clave o un password pueden traer "=" adentro; partir en el primero es
        # lo correcto, partir en el ultimo tiraria el valor.
        self.assertEqual(parse_env_file(_write("A=x=y=z\n")), {"A": "x=y=z"})

    def test_no_evalua_el_valor(self):
        ruta = _write("A=$(echo injection)\nB=`id`\n")
        self.assertEqual(parse_env_file(ruta), {"A": "$(echo injection)", "B": "`id`"})

    def test_una_linea_sin_igual_se_salte(self):
        self.assertEqual(parse_env_file(_write("esto no es una asignacion\nA=1\n")), {"A": "1"})

    def test_un_comentario_inline_no_parte_el_valor(self):
        # "A=1 # nota" da el valor "1 # nota": partir en el "#" cortaria una clave
        # valida. Las claves de API no tienen "#", pero un comentario de una linea
        # con "#" al final es error suficiente como para no intentar adivinar.
        self.assertEqual(parse_env_file(_write("A=1 # nota\n")), {"A": "1 # nota"})

    def test_ultima_linea_gana_si_repite_la_clave(self):
        self.assertEqual(parse_env_file(_write("A=1\nA=2\n")), {"A": "2"})


class TestPrecedencia(unittest.TestCase):
    def test_la_variable_de_entorno_gana(self):
        ruta = _write("DEEPSEEK_API_KEY=del-archivo\n")
        env = {"DEEPSEEK_API_KEY": "del-entorno"}
        self.assertEqual(load_env(ruta, env), {})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "del-entorno")

    def test_sin_variable_carga_del_archivo(self):
        ruta = _write("DEEPSEEK_API_KEY=del-archivo\n")
        env: dict[str, str] = {}
        self.assertEqual(load_env(ruta, env), {"DEEPSEEK_API_KEY": "del-archivo"})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "del-archivo")

    def test_una_variable_vacia_tambien_se_pisa(self):
        # Exportar la variable en blanco es un estado comun ("la perdi") y tiene
        # que caer al archivo, no quedar en un Authorization vacio.
        ruta = _write("DEEPSEEK_API_KEY=del-archivo\n")
        env = {"DEEPSEEK_API_KEY": ""}
        load_env(ruta, env)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "del-archivo")

    def test_una_variable_de_espacios_tambien_se_pisa(self):
        env = {"DEEPSEEK_API_KEY": "   "}
        load_env(_write("A=1\nDEEPSEEK_API_KEY=x\n"), env)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "x")

    def test_sin_archivo_no_falla(self):
        env: dict[str, str] = {}
        self.assertEqual(load_env(Path("no-existe-este.env"), env), {})
        self.assertEqual(env, {})

    def test_un_archivo_ilegible_no_falla(self):
        # Se simula con un mock y no con ``chmod``: en Windows los permisos POSIX no
        # impiden la lectura, asi que el test pasaria sin ejercitar nada.
        ruta = _write("A=1\n")
        env: dict[str, str] = {}
        original = Path.read_text

        def revienta(self, *args, **kwargs):
            raise PermissionError("acceso denegado")

        Path.read_text = revienta
        try:
            self.assertEqual(load_env(ruta, env), {})
            self.assertEqual(env, {})
        finally:
            Path.read_text = original

    def test_no_toca_el_environ_real_si_se_pasa_env(self):
        os.environ.pop("PREPDATA_TEST_VAR", None)
        env: dict[str, str] = {}
        load_env(_write("PREPDATA_TEST_VAR=1\n"), env)
        self.assertEqual(env["PREPDATA_TEST_VAR"], "1")
        self.assertNotIn("PREPDATA_TEST_VAR", os.environ)


class TestUbicacion(unittest.TestCase):
    def test_busca_en_la_raiz_del_proyecto(self):
        # La raiz, no el directorio desde el que se corre: si se buscara en el cwd,
        # correr el script desde otro lado dejaria de encontrar la clave.
        self.assertEqual(default_env_path(), project_root() / ".env")
        self.assertEqual(default_env_path().parent, project_root())

    def test_el_env_real_esta_ignorado(self):
        raiz = project_root()
        ignorado = (raiz / ".gitignore").read_text(encoding="utf-8")
        lineas = {x.strip() for x in ignorado.splitlines()}
        self.assertIn(".env", lineas)
        self.assertIn("!.env.example", lineas)
    def test_la_plantilla_no_tiene_una_clave_puesta(self):
        texto = (project_root() / ".env.example").read_text(encoding="utf-8")
        for var in ("OPENCODE_API_KEY", "DEEPSEEK_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY"):
            claves = [x for x in texto.splitlines() if x.startswith(f"{var}=")]
            self.assertEqual(claves, [f"{var}="], f"la plantilla no debería traer {var} cargada")
