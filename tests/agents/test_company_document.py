import io
import unittest
import zipfile
from src.ai.company_document import DocumentError, extract_document, MAX_UPLOAD_BYTES, MAX_TEXT_CHARS


def docx(xml, extra=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', xml)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return out.getvalue()


class CompanyDocumentTests(unittest.TestCase):
    def test_utf8_and_bom(self):
        self.assertEqual(extract_document('company.TXT', '\ufeffКомпания\r\nHelp'.encode('utf-8')), 'Компания\nHelp')
        self.assertEqual(extract_document('rules.md', b'# Rules\n\nBe clear.'), '# Rules\n\nBe clear.')

    def test_word_paragraphs_and_table_text(self):
        raw = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Company</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Refunds</w:t><w:tab/><w:t>14 days</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
        self.assertEqual(extract_document('company.docx', docx(raw)), 'Company\nRefunds\t14 days')

    def test_rejects_wrong_and_empty_formats(self):
        for name, content in [('old.doc', b'abc'), ('empty.txt', b' '), ('binary.txt', b'a\x00b'), ('latin.txt', b'\xff'), ('bad.docx', b'not zip')]:
            with self.subTest(name=name), self.assertRaises(DocumentError):
                extract_document(name, content)

    def test_bounds_file_and_extracted_text(self):
        for data in [b'x'*(MAX_UPLOAD_BYTES+1), b'x'*(MAX_TEXT_CHARS+1)]:
            with self.assertRaises(DocumentError):
                extract_document('large.txt', data)

    def test_rejects_entities_macros_zip_bombs_and_excess_entries(self):
        cases = [docx('<!DOCTYPE x [<!ENTITY test "bad">]><x>&test;</x>'),
                 docx('<x/>', {'word/vbaProject.bin': 'macro'}),
                 docx('<x/>', {'large': 'x'*(8*1024*1024+1)}),
                 docx('<x/>', {f'file{i}': '' for i in range(256)})]
        for case in cases:
            with self.assertRaises(DocumentError):
                extract_document('bad.docx', case)


if __name__ == '__main__':
    unittest.main()
