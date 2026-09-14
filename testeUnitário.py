import unittest

import Teste


class TestInvoiceHelpers(unittest.TestCase):
    def test_clean_text_removes_whitespace(self):
        self.assertEqual(Teste.CleanText("  ABC  "), "ABC")
        self.assertEqual(Teste.CleanText(None), "")

    def test_normalize_invoice_reference_from_url(self):
        self.assertEqual(
            Teste.NormalizeInvoiceReference("https://rpachallengeocr.azurewebsites.net/invoices/12.jpg"),
            "12.jpg",
        )
        self.assertEqual(Teste.NormalizeInvoiceReference("7"), "7.jpg")
        self.assertEqual(Teste.NormalizeInvoiceReference("abc"), "")

    def test_parse_date_value_supports_multiple_formats(self):
        self.assertEqual(Teste.ParseDateValue("15/09/2024").strftime("%d-%m-%Y"), "15-09-2024")
        self.assertEqual(Teste.ParseDateValue("2024-09-15").strftime("%Y-%m-%d"), "2024-09-15")
        self.assertIsNone(Teste.ParseDateValue(""))

    def test_extract_fields_regex(self):
        text = (
            "Invoice No: INV-1023\n"
            "Invoice Date: 15/09/2024\n"
            "Company: Acme Ltd\n"
            "Bill To: Beta Company\n"
            "Total Due: $1,250.00"
        )

        result = Teste.ExtractFieldsRegex(text)

        self.assertEqual(result["invoice_number"], "INV-1023")
        self.assertEqual(result["invoice_date"], "15/09/2024")
        self.assertEqual(result["company_name"], "Acme Ltd")
        self.assertEqual(result["company_bill"], "Beta Company")
        self.assertEqual(result["total_due"], "1,250.00")

    def test_extract_fields_regex_more_flexible(self):
        text = (
            "Invoice No INV-2024\n"
            "Date 15/09/2024\n"
            "Company: Acme Industry Bill To: Beta Company\n"
            "Due 1250.00"
        )

        result = Teste.ExtractFieldsRegex(text)

        self.assertEqual(result["invoice_number"], "INV-2024")
        self.assertEqual(result["invoice_date"], "15/09/2024")
        self.assertEqual(result["company_name"], "Acme Industry")
        self.assertEqual(result["company_bill"], "Beta Company")
        self.assertEqual(result["total_due"], "1250.00")

    def test_extract_invoice_names_from_data(self):
        payload = {
            "items": [
                {"invoice": "https://site.com/invoices/8.jpg"},
                {"id": "9"},
                {"filename": "10.png"},
            ]
        }

        result = Teste.ExtractInvoiceNamesFromData(payload)

        self.assertIn("8.jpg", result)
        self.assertIn("9.jpg", result)
        self.assertIn("10.png", result)

    def test_extract_due_date_and_id_from_data(self):
        payload = {
            "rows": [
                {
                    "id": "42",
                    "due_date": "18/09/2024",
                    "invoice": "https://site.com/invoices/42.jpg",
                }
            ]
        }

        result = Teste.ExtractDueDateAndIdFromData(payload)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "42")
        self.assertEqual(result[0]["due_date"], "18/09/2024")
        self.assertEqual(result[0]["invoice_file"], "42.jpg")

    def test_fatura_to_csv(self):
        fatura = Teste.Fatura(
            id_fatura="42",
            data_vencimento="18-09-2024",
            numero="INV-42",
            data_emissao="15-09-2024",
            empresa="Acme",
            empresa_destino="Beta",
            valor_total="1500.00",
        )

        row = fatura.ToCsv()

        self.assertEqual(row["ID"], "42")
        self.assertEqual(row["CompanyName"], "Acme")
        self.assertEqual(row["CompanyBill"], "Beta")
        self.assertEqual(row["TotalDue"], "1500.00")


if __name__ == "__main__":
    unittest.main()
