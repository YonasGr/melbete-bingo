import pytest
from src.services.sms_parser import extract_verification_url
from src.services.wallet_service import WalletService, DuplicateTransactionError, InvalidAmountError
from src.repositories.transaction_repository import TransactionRepository
from src.repositories.wallet_repository import WalletRepository


def test_extract_verification_url_telebirr_amharic():
    sms = (
        "ውድKedir\n"
        "ወደ Yonas Girma(25193587) 50.00 ብር በ 23/07/2026 19:11:01 ልከዋል። የሂሳብ እንቅስቃሴ ቁጥርዎ DGN26MCD4K ነዉ። "
        "የአገልግሎት ክፍያው 0.87 ብር ፤ የአገልግሎት ክፍያው 15% VAT 0.13 ብር ነዉ። አሁን ያለዎት ቀሪ ሂሳብ 0.00 ብር ነው። "
        "የክፍያ መረጃዎን ለማግኘት ማስፈንጠርያውን ይጫኑ፡ https://transactioninfo.ethiotelecom.et/receipt/DGN26MCD4K\n"
        "በቴሌብር ስለተገለገሉ እናመሰግናለን\n"
        "ኢትዮ ቴሌኮም (466869133)"
    )
    url = extract_verification_url(sms)
    assert url == "https://transactioninfo.ethiotelecom.et/receipt/DGN26MCD4K"


def test_extract_verification_url_telebirr_english():
    sms = (
        "Dear Yonas\n"
        "You have transferred ETB 3,740.00 to Amanuel Kefelegn (2519****7740) on 23/07/2026 19:48:19. "
        "Your transaction number is DGN76O4LCF. The service fee is ETB 5.22 and 15% VAT on the service fee is ETB 0.78. "
        "Your current E-Money Account balance is ETB 2,636.09. To download your payment information please click this link: "
        "https://transactioninfo.ethiotelecom.et/receipt/DGN76O4LCF.\n\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )
    url = extract_verification_url(sms)
    assert url == "https://transactioninfo.ethiotelecom.et/receipt/DGN76O4LCF"


def test_extract_verification_url_cbe_birr():
    sms = (
        "Dear yonas, you have sent 855.00Br. to TERMO NIGUSE on 09/02/26 20:50,Txn ID DB9515M7WP3. "
        "Your CBE Birr account balance is 790.50Br.Thank you! For invoice https://cbepay1.cbe.com.et/aureceipt?TID=DB9515M7WP3&PH=251909823587 "
        "For your feedback please click the link https://shorturl.at/gy3A0"
    )
    url = extract_verification_url(sms)
    assert url == "https://cbepay1.cbe.com.et/aureceipt?TID=DB9515M7WP3&PH=251909823587"


def test_extract_verification_url_no_url():
    sms = "You sent 500 ETB to John Doe. Thank you!"
    url = extract_verification_url(sms)
    assert url is None
