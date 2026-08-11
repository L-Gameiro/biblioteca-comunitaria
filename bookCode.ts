/**
 * bookCode.ts
 * ---------------------------------------------------------------------------
 * Gera o código de identificação único de um livro a partir do nome do autor
 * e da contagem de livros já cadastrados para aquele autor.
 *
 * REGRA (conforme especificação):
 *   [3 primeiras letras do "último token" do nome do autor, maiúsculas]
 *   + [1ª letra do primeiro nome, maiúscula]
 *   - [número sequencial de 3 dígitos para aquele autor]
 *
 * Exemplo dado na especificação:
 *   "João Mellão Neto", 1º livro  ->  "NETJ-001"
 *   (o código usa "Neto", o ÚLTIMO token do nome, não "Mellão")
 *
 * NOTA DE DESIGN — ambiguidade da especificação:
 * Sufixos geracionais (Neto, Filho, Júnior/Jr, Sobrinho) podem ser tratados
 * de duas formas na catalogação de nomes brasileiros:
 *   (a) como o próprio "sobrenome" para fins de código (é o que o exemplo
 *       da especificação faz: "Neto" -> NET) — comportamento PADRÃO aqui;
 *   (b) como um sufixo que deve ser ignorado, usando o sobrenome anterior
 *       ("Mellão" -> MEL) — disponível via a opção `treatSuffixAsSurname: false`.
 * Por padrão a função replica exatamente o exemplo da especificação (opção a).
 */

/** Sufixos geracionais reconhecidos (comparados sem acento, maiúsculos). */
const GENERATIONAL_SUFFIXES = new Set([
  "NETO",
  "FILHO",
  "JUNIOR",
  "JR",
  "SOBRINHO",
]);

export interface GenerateBookCodeOptions {
  /**
   * Se true (padrão), replica o exemplo da especificação: usa sempre o
   * último token do nome como base do código, mesmo que seja um sufixo
   * geracional (Neto, Filho, Júnior, Sobrinho).
   * Se false, quando o último token for um sufixo geracional conhecido,
   * usa o token anterior (o sobrenome "de verdade") como base.
   */
  treatSuffixAsSurname?: boolean;
}

/** Remove acentos/diacríticos de uma string (á -> a, ç -> c, ã -> a, etc). */
function stripDiacritics(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

/** Mantém apenas letras A-Z (maiúsculas) de uma string já sem acentos. */
function onlyUpperLetters(value: string): string {
  return stripDiacritics(value).toUpperCase().replace(/[^A-Z]/g, "");
}

/**
 * Gera o código de um livro a partir do nome completo do autor e da
 * quantidade de livros já cadastrados para esse autor (0 para o primeiro).
 *
 * @param authorFullName        Nome completo do autor (ex: "João Mellão Neto")
 * @param existingCountForAuthor Quantos livros desse autor já existem no catálogo
 *                                (0 = este será o livro nº 1)
 * @param options                Ver GenerateBookCodeOptions
 * @returns Código no formato "XXXY-NNN", ex: "NETJ-001"
 */
export function generateBookCode(
  authorFullName: string,
  existingCountForAuthor: number,
  options: GenerateBookCodeOptions = {}
): string {
  const { treatSuffixAsSurname = true } = options;

  if (typeof authorFullName !== "string" || authorFullName.trim() === "") {
    throw new Error("Nome do autor não pode ser vazio.");
  }
  if (
    !Number.isInteger(existingCountForAuthor) ||
    existingCountForAuthor < 0
  ) {
    throw new Error("existingCountForAuthor deve ser um inteiro >= 0.");
  }

  const parts = authorFullName.trim().split(/\s+/).filter(Boolean);

  const firstName = parts[0];
  let surnameToken = parts[parts.length - 1];

  // Autor com um único nome (ex: "Homero"): o mesmo token vira
  // sobrenome-base e também fornece a inicial do primeiro nome.
  if (!treatSuffixAsSurname && parts.length > 1) {
    const asSuffix = onlyUpperLetters(surnameToken);
    if (GENERATIONAL_SUFFIXES.has(asSuffix)) {
      surnameToken = parts[parts.length - 2];
    }
  }

  let surnameCode = onlyUpperLetters(surnameToken);
  // Sobrenomes com menos de 3 letras (raro, mas existe: "Li", "Wu", "Eco"
  // com acento removido etc.) são completados com "X" para manter 3 chars.
  if (surnameCode.length < 3) {
    surnameCode = surnameCode.padEnd(3, "X");
  }
  surnameCode = surnameCode.slice(0, 3);

  const firstInitial = onlyUpperLetters(firstName).charAt(0) || "X";

  // Sequência de 3 dígitos; cresce naturalmente além de 999 se necessário
  // (ex: 1000º livro do mesmo autor -> "1000", não trunca).
  const sequence = String(existingCountForAuthor + 1).padStart(3, "0");

  return `${surnameCode}${firstInitial}-${sequence}`;
}
